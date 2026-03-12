# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **live trading bot** for the Boros protocol (Pendle) on Arbitrum One. It executes FR (Funding Rate) Arbitrage — entering paired positions across two YU markets with the same underlying but different maturities when the implied spread exceeds a threshold.

**This is NOT the backtest platform.** The backtest/research repo is the parent directory (`../`). This repo has its own framework in `strategies/framework/` for live execution.

**Key domain concept:** YU Position Value = Implied APR × Time Remaining × Spot Price. The bot profits from spread convergence between paired YU markets while managing time decay.

## Commands

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_executor.py -v

# Run dry-run simulation (no real trades)
python3 run_sim.py

# Run live trading (requires agent keystore + .env)
python3 main.py --live

# Run with custom interval
python3 main.py --interval 120

# pip install (must use proxy flags)
pip install <pkg> --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org
```

## Architecture

### Execution Modes

Two modes controlled by `main.py`:

| Mode | Context | State | Signing | Entry Point |
|------|---------|-------|---------|-------------|
| **Dry-run** | `LiveContext(dry_run=True)` | `JsonFileStateManager` → `bot_state.json` | None | `main.py` or `run_sim.py` |
| **Live** | `ProdContext(...)` | `ApiStateManager` → on-chain recovery | `AgentSigner` (EIP-712) | `main.py --live` |

### Strategy: FR Arbitrage (`strategies/fr_arb/`)

**`strategy.py`** — `FRArbitrageStrategy.on_tick(context)` runs each tick:

1. **Prefetch:** Parallel orderbook fetch (8 workers), collateral sync, on-chain state sync
2. **Pair generation:** Groups markets by (base_asset, maturity, tokenId), pairs older/newer maturities
3. **Per-pair state machine:**
   - Both positions open → check exit (spread < threshold AND hold >= min hours)
   - One position (orphan) → close it
   - No positions → check entry (spread > threshold, margin pre-check, capacity walk)
4. **Atomic execution:** Dual-market orders via `submit_dual_order()` — both legs succeed or both fail
5. **On-chain sync:** Each tick rebuilds state from `/collaterals/summary` (source of truth)

**`config.py`** — All parameters support env var overrides. Key thresholds: `ENTRY_SPREAD_THRESHOLD=0.042`, `EXIT_SPREAD_THRESHOLD=0.038`.

### Framework (`strategies/framework/`)

This is the **live trading framework** (NOT backtest shims — those are in the parent repo). Key components:

| File | Class | Purpose |
|------|-------|---------|
| `interfaces.py` | `IDataProvider`, `IExecutor`, `IStateManager`, `IContext` | Abstract contracts for DI |
| `runner.py` | `StrategyRunner` | Tick loop, circuit breaker (5 failures → 5 min pause), PnL tracking, JSONL logging |
| `executor.py` | `BorosExecutor` | Get calldata from API → EIP-712 sign → submit to `/v2/agent/bulk-direct-call` |
| `data_provider.py` | `BorosDataProvider` | Orderbooks, market info, pair generation, collateral queries |
| `state_manager.py` | `JsonFileStateManager`, `ApiStateManager` | Position persistence (file vs API recovery) |
| `signing.py` | `AgentSigner` | EIP-712 signing, `pack_account()` (21-byte), `derive_cross_market_acc()` (26-byte) |
| `pricing.py` | `PricingEngine` | IM calculation, limit tick conversion |
| `alert.py` | `AlertHandler` | IFTTT webhooks with priority levels (P0-P3) and throttling |
| `secrets.py` | `load_secrets()` | `.env` / AWS Secrets Manager / GCP Secret Manager |
| `keystore.py` | `load_agent_key()` | Password-protected Ethereum keystore (scrypt) |
| `context.py` | `LiveContext`, `ProdContext` | Wires together executor + data provider + state manager |

### Agent Authorization (`approve_agent.py`)

One-time setup to authorize an agent address to trade on behalf of a root wallet. Three modes:
- **Direct:** Sign with root private key (`--root-key`)
- **Manual:** Display EIP-712 message, paste signature
- **QR:** BC-UR animated QR codes for air-gapped hardware wallets (uses `bc_ur.py`)

### Key API Endpoints (Boros)

- `GET /v1/markets`, `GET /v1/markets/{id}` — Market info
- `GET /v1/order-books/{id}?tickSize=0.001` — Orderbook
- `GET /v2/markets/indicators` — Funding rates
- `GET /v1/collaterals/summary?userAddress=...` — Balances + positions
- `POST /v4/calldata/place-order`, `/v1/calldata/dual-market-place-order`, `/v4/calldata/close-active-position` — Get calldata
- `POST /v2/agent/bulk-direct-call` — Submit signed transactions (atomic with `requireSuccess=true`)
- `POST /v1/agent/approve` — Agent authorization

Critical API behavior documented in `docs/boros_api_corrections.md` — read before modifying executor or signing logic.

### Deployment

Docker Compose with two services:
- **prod:** `main.py --live`, restart always, 180MB memory limit, persistent logs/entry_times
- **test:** dry-run with `--profile test`, restart no, 400MB limit

Secrets via `SECRET_SOURCE` env var: `env` (default, `.env` file), `aws`, or `gcp`.

## Key Design Patterns

- **On-chain state as source of truth:** Bot recovers from disconnections by syncing positions from API each tick
- **Atomic dual-market orders:** Both legs in single POST, `requireSuccess=true` prevents partial fills
- **Collateral deduction cache:** After each entry, estimated IM deducted from cached balance to prevent over-trading within same tick
- **Capacity stepping:** Entry size walks in 5-token increments until spread falls below threshold or book exhausted
- **Circuit breaker:** 5 consecutive failures → 5 minute cooldown
- **Event-driven:** Strategy returns events list; runner dispatches to logger + alert handler

## Conventions

- **No Chinese comments in code** — all comments in English
- **Never use /tmp** — use paths within project directory
- **Run tests after modifying:** executor, signing, state management, or strategy logic
- **State files** (`*.json`) and `logs/` are gitignored
- `.env` files contain secrets — never commit
- Strategy events: `entry`, `exit`, `scan`, `hold`, `skip`, `exec_fail`, `liquidation`, `summary`

## Key Constants

```python
ROUTER_ADDRESS = "0x8080808080daB95eFED788a9214e400ba552DEf6"  # Boros Router on Arbitrum
ARBITRUM_CHAIN_ID = 42161
```
