# Boros API Corrections & Supplements

Corrections to MCP knowledge base (pendle-qa) based on live production testing.
Each section references the MCP claim and the verified behavior.

---

## 1. close-active-position `side` Parameter [CRITICAL]

**MCP Claim (Calls #41, #42, #44, #45):**
> "side parameter refers to the side of the position being closed,
> where 0 represents LONG and 1 represents SHORT"
> "These semantics are consistent throughout the Boros Protocol"

**Actual Behavior (verified live 2026-03-10):**

The `side` parameter is the **closing order direction** (OPPOSITE of position side):
- To close a **SHORT** position (side=1): pass `side=0` (LONG order to close)
- To close a **LONG** position (side=0): pass `side=1` (SHORT order to close)

Passing the position's own side results in HTTP 400 `ORDER_WRONG_SIDE`.

**Evidence:**
```
# WRONG: passing position side → both legs rejected
Close calldata params: {'marketId': 41, 'side': 1, ...}  → HTTP 400 ORDER_WRONG_SIDE
Close calldata params: {'marketId': 63, 'side': 0, ...}  → HTTP 400 ORDER_WRONG_SIDE

# CORRECT: passing opposite side → both legs success
Close calldata params: {'marketId': 41, 'side': 0, ...}  → 200 OK
Close calldata params: {'marketId': 63, 'side': 1, ...}  → 200 OK
bulk-direct-call response: [{"status": "success", ...}, {"status": "success", ...}]
```

**Code fix:**
```python
# In _get_close_position_calldata:
close_side = 1 - int(side)  # flip: SHORT(1) → 0, LONG(0) → 1
params = {"marketId": market_id, "side": close_side, ...}
```

---

## 2. `side` Semantics Are NOT Consistent Across Endpoints

**MCP Claim (Call #44):**
> "These semantics are consistent throughout the Boros Protocol.
> Resulting positions tracked via AccountsController_getActivePositions
> use the same numeric values to represent the position side.
> When closing a position, you must provide the side corresponding
> to the position being closed (0 for LONG, 1 for SHORT)."

**Actual Behavior:**

| Endpoint | `side` meaning | SHORT | LONG |
|---|---|---|---|
| `place-order` | Order direction | 1 | 0 |
| `dual-market-place-order` | Order direction | 1 | 0 |
| `collaterals/summary` marketPositions | Position side | 1 | 0 |
| `close-active-position` | **Closing order direction** | **0** | **1** |

The close endpoint is the odd one out — it uses the reverse convention.

---

## 3. `marketAcc` Format for Cross-Margin

**MCP Claim (Calls #30, #31):**
> "marketAcc is a required string (20-byte address)"
> "API expects standard 20-byte Ethereum-style address"

**Actual Behavior:**

For **cross-margin** accounts, `marketAcc` is **26 bytes** (52 hex chars), not 20:

```
Format: 0x + root_address(20 bytes) + accountId_tokenId(3 bytes) + 0xffffff(3 bytes)

Example:
  root_address:    0xf2ea6337a8d4da4173eb1976709aaf0fc3c96bc0  (20 bytes)
  accountId=0, tokenId=2:  000002                                (3 bytes)
  cross-margin marker:     ffffff                                (3 bytes)

  marketAcc = 0xf2ea6337a8d4da4173eb1976709aaf0fc3c96bc0000002ffffff
```

Derivation code:
```python
def derive_cross_market_acc(root_address, token_id, account_id=0):
    addr_hex = root_address.lower().replace("0x", "")
    acc_token = (account_id << 16) | token_id
    acc_token_hex = f"{acc_token:06x}"
    return f"0x{addr_hex}{acc_token_hex}ffffff"
```

Both `place-order` and `close-active-position` accept this 26-byte format.
The `dual-market-place-order` also uses it in the `marketAcc` body field.

---

## 4. EIP-712 Signing Details (Missing from MCP)

**MCP Claim (Calls #19, #20, #37):**
> "No specific EIP-712 domain separator fields provided in resources"
> "UNABLE TO ACCESS: specific EIP-712 domain types"

**Verified working configuration:**

```python
# Domain
EIP712_DOMAIN = {
    "name": "Pendle Boros Router",
    "version": "1.0",
    "chainId": 42161,  # Arbitrum One
    "verifyingContract": "0x8080808080daB95eFED788a9214e400ba552DEf6",
}

# Types — note bytes21 for account, not bytes32
EIP712_TYPES = {
    "PendleSignTx": [
        {"name": "account", "type": "bytes21"},
        {"name": "connectionId", "type": "bytes32"},
        {"name": "nonce", "type": "uint64"},
    ],
}

# account = pack(root_address, account_id)
# 21 bytes: (root_int << 8) | account_id
account = (int(root_address, 16) << 8 | account_id).to_bytes(21, "big")

# connectionId = keccak256(calldata)
connection_id = Web3.keccak(hexstr=calldata)

# nonce = millisecond timestamp * 1000 + index
base_nonce = int(time.time() * 1000) * 1000
nonce = base_nonce + index  # for bulk signing
```

**Key details MCP couldn't provide:**
- `account` type is `bytes21` (not bytes32)
- `nonce` type is `uint64` (not uint256)
- `connectionId` is `keccak256(calldata)` (not a random nonce)
- For bulk signing, nonce increments per calldata: `base + 0`, `base + 1`, etc.

---

## 5. Wei Precision and Token Amount Handling

**Not addressed by MCP at all.**

API returns token sizes as BigInt strings in wei (18 decimals):
```json
{"notionalSize": "13006825273831940096"}
```

**Critical rule:** Never round-trip through float64:
```python
# WRONG — loses precision, leaves dust on close:
tokens = int(notional) / 1e18       # 13.006825273831940 (precision loss)
size_wei = str(int(tokens * 1e18))  # "13006825273831939072" (off by 1024)

# CORRECT — preserve original wei string:
tokens_wei = str(abs(int(notional)))  # "13006825273831940096" (exact)
tokens = int(notional) / 1e18        # float for display/math only
# Use tokens_wei directly when calling close-active-position
```

This applies to all API fields returning BigInt strings:
`notionalSize`, `availableBalance`, `initialMargin`, `totalNetBalance`, etc.

---

## 6. PnL Formula Uses Time-to-Maturity, Not Holding Time

**Not addressed by MCP.**

Boros positions are fixed-rate swaps. PnL depends on **remaining contract life**:

```
# WRONG:
uPnL = (entry_rate - mark_rate) * tokens * hold_years * spot_price

# CORRECT:
uPnL = (entry_rate - mark_rate) * tokens * time_to_maturity_years * spot_price
```

Where `time_to_maturity_years = max(0, (maturity_timestamp - now) / 31_536_000)`.

The maturity timestamp comes from `market.imData.maturity` (unix seconds).

**For SHORT positions:** profit when rate goes down
```
uPnL_short = (entry_rate - mark_rate) * tokens * TTM * spot
```

**For LONG positions:** profit when rate goes up
```
uPnL_long = (mark_rate - entry_rate) * tokens * TTM * spot
```

---

## 7. `collaterals/summary` Response Structure

**MCP Claim (Call #40):**
> Top-level key: `"collaterals"` (NOT "results")

**Verified correct.** The response uses `collaterals` not `results`:
```json
{
  "collaterals": [
    {
      "tokenId": 2,
      "crossPosition": {
        "availableBalance": "481407000000000000",
        "marketPositions": [
          {
            "marketId": 41,
            "side": 1,
            "notionalSize": "13006825273831940096",
            "fixedApr": 0.032983,
            "markApr": 0.047385,
            "pnl": {"unrealisedPnl": "-8553000000000000"}
          }
        ]
      }
    }
  ]
}
```

Key fields:
- `side`: integer (0=LONG, 1=SHORT) — this IS the position side
- `notionalSize`: BigInt string in wei (preserve exact value!)
- `fixedApr`: float, the entry fixed rate
- `pnl.unrealisedPnl`: BigInt string in wei, denominated in collateral token

---

## 8. `bulk-direct-call` Status Verification

**MCP Claim (Call #28):**
> "Returns: array of status objects per transaction"
> "Fields: error (string), index (number), status (string)"

**Additional critical detail:**

ALL entries must have `status == "success"` for the batch to be considered successful.
Any other status (`"reverted"`, `"pending"`, `""`, missing) means failure:

```python
for d in data:
    if d.get("status") != "success":
        return None  # entire batch failed
```

When using `requireSuccess=true` in the payload, a single failure reverts the
entire batch (atomic execution). Both calldatas share the same txHash.

---

## 9. `close-active-position` Required Parameters

**MCP Claim (Call #42):**
> Required: marketId, side, size (bigint string), tif, marketAcc

**Verified — all five are needed.** Additionally:

- `slippage` (float): Required for market orders (like `place-order`).
  Without it, the API may reject with "slippage is required".
- `size`: Must be exact wei string. For full close, use the original
  `notionalSize` from the position to avoid dust.

Working example:
```python
params = {
    "marketId": 41,
    "side": 0,              # closing direction, NOT position side
    "size": "13006825273831940096",  # exact wei from position data
    "tif": 2,               # FOK
    "slippage": 0.05,       # 5% price protection
    "marketAcc": "0xf2ea...000002ffffff",  # 26-byte cross-margin format
}
```

---

## 10. `approve-agent` Returns HTTP 201

**Not mentioned by MCP.**

The agent approval endpoint returns HTTP **201** (Created) on success, not 200.
Code must check for both:

```python
if resp.status_code in (200, 201):
    # approval successful
```

---

## 11. `dual-market-place-order` Uses Single `marketAcc`

**MCP Claim (Call #35):**
> Request body: marketAcc, order1, order2

**Verified.** Only ONE `marketAcc` for both legs. Both markets must share the
same collateral pool (same `tokenId`). The `marketAcc` is derived from the
first market's `tokenId`:

```python
market_acc = derive_cross_market_acc(root_address, token_id_from_market_a)
body = {
    "marketAcc": market_acc,
    "order1": {"marketId": mkt_a, "side": 1, "size": wei_str, "tif": 2, "slippage": 0.05},
    "order2": {"marketId": mkt_b, "side": 0, "size": wei_str, "tif": 2, "slippage": 0.05},
}
```

For FR arbitrage, paired markets always share `tokenId` (same underlying asset,
different maturities), so a single `marketAcc` works for both.

---

## 12. Orderbook Endpoint and Response Format

**MCP Claim (Call #3):**
> Core API: `GET /v1/order-books/{marketId}`
> Open-Api: `GET /v2/markets/order-books` (RECOMMENDED)

**Actual usage:**

The bot uses core API `/v1/order-books/{marketId}` successfully.
The `tickSize` query parameter is **required** but not mentioned by MCP:

```
GET /v1/order-books/{marketId}?tickSize=0.001
```

Response format uses `long`/`short` (not `bids`/`asks`):
```json
{
  "long": {"ia": [42, 41, 40], "sz": ["5000000000000000000", ...]},
  "short": {"ia": [43, 44, 45], "sz": ["3000000000000000000", ...]}
}
```

- `long` side = bids (buyers of yield, pay fixed rate)
- `short` side = asks (sellers of yield, receive fixed rate)
- `ia` = tick indices (integers), rate = `ia * tickSize`
- `sz` = size per level in wei (BigInt strings, 18 decimals)

---

## 13. `/v1/pnl/transactions` Response Format

**MCP Claim (Call #39):**
> Use `GET /v1/accounts/transactions` (AccountsController_getTransactions)

**Actual endpoint used:** `GET /v1/pnl/transactions` (different controller).

Response format is **inconsistent** — sometimes an array, sometimes `{results: [...]}`:
```python
# Must handle both formats:
data = api_response.json()
trades = data if isinstance(data, list) else data.get("results", [])
```

Parameters: `userAddress`, `accountId`, `marketId`, `limit`.
Each trade object contains: `marketId`, `time` (unix timestamp), `side`,
`notionalSize`, `fixedApr`.

---

## 14. `tif` (Time-in-Force) Values

**MCP Claim (Call #35):**
> "tif (0=GTC, 1=IOC, 2=...)" (truncated, incomplete)

**Complete verified values:**

| Value | Name | Behavior |
|---|---|---|
| 0 | GTC | Good-Till-Cancel (resting order) |
| 1 | IOC | Immediate-Or-Cancel (partial fill ok) |
| 2 | FOK | Fill-Or-Kill (full fill or cancel) |
| 3 | ALO | Add-Liquidity-Only (maker only) |
| 4 | SALO | ? (undocumented) |

The bot uses **FOK (tif=2)** for all orders to prevent partial fills.

---

## 15. `size` Parameter in Place-Order Endpoints

**MCP did not clearly specify the format.**

The `size` parameter across all calldata endpoints is a **BigInt string in wei**
(18 decimals), NOT a float:

```python
# CORRECT:
"size": "13006825273831940096"   # BigInt string

# WRONG:
"size": 13.0068                 # float
"size": "13.0068"               # float string
```

Conversion from token amount:
```python
size_wei = str(int(tokens * 1e18))  # for opening (truncation ok)
size_wei = stored_tokens_wei        # for closing (use exact original)
```

---

## 16. Market Info `imData` Key Fields

**MCP mentioned IM formula but not field locations.**

Key fields from `GET /v1/markets/{marketId}`:
```json
{
  "marketId": 41,
  "tokenId": 2,
  "state": "Normal",
  "imData": {
    "name": "...",
    "symbol": "BINANCE-ETHUSDT-26JUN2026",
    "maturity": 1782172800,
    "tickStep": 1,
    "iTickThresh": 100,
    "marginFloor": 0
  },
  "config": {
    "kIM": "1000000000000000000",
    "tThresh": 604800
  },
  "data": {
    "bestBid": 0.045,
    "bestAsk": 0.046,
    "markApr": 0.0455,
    "assetMarkPrice": 2065.5,
    "floatingApr": 0.03
  }
}
```

Notes:
- `kIM` in `config`: BigInt string with 18 decimals (divide by 1e18 for float)
- `tThresh` in `config`: seconds (NOT in `imData`)
- `maturity` in `imData`: unix timestamp
- `state`: must be `"Normal"` for active markets
- `assetMarkPrice` in `data`: spot price of underlying (e.g., ETH in USD)

---

## 17. Pair Generation: Symbol Format

**Not mentioned by MCP.**

Market symbols follow the pattern: `PLATFORM-TICKER-EXPIRY`
```
BINANCE-ETHUSDT-26JUN2026
BINANCE-BTCUSDT-27MAR2026
```

To pair markets for arbitrage:
1. Parse base asset: strip platform prefix and USDT/USDC suffix
2. Group by `(base_asset, maturity, tokenId)` — same collateral pool
3. Only pair markets with `state == "Normal"` and `maturity > now`
