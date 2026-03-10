"""
Strategy Runner - main loop that ticks strategies at a configurable interval.
Supports running multiple strategies concurrently.
Writes structured JSONL logs for post-analysis.
Includes circuit breaker to pause on consecutive failures.
Periodic summary with PnL tracking.
"""
import json
import logging
import os
import time
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .alert import AlertHandler, IFTTTAlert
from .base_strategy import BaseStrategy
from .interfaces import IContext

logger = logging.getLogger(__name__)

# Circuit breaker constants
CB_FAILURE_THRESHOLD = 5      # consecutive tick failures to trip
CB_COOLDOWN_SECONDS = 300     # 5 minutes cooldown when tripped

# Summary interval (every N ticks)
SUMMARY_INTERVAL = 10

TOKEN_NAMES = {1: "WBTC", 2: "WETH", 3: "USDT", 4: "BNB", 5: "HYPE"}


class CircuitBreaker:
    """Simple circuit breaker: CLOSED -> OPEN after N consecutive failures."""

    def __init__(self, failure_threshold: int = CB_FAILURE_THRESHOLD,
                 cooldown_seconds: int = CB_COOLDOWN_SECONDS):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_failures = 0
        self.open_until: float = 0.0  # timestamp when CB closes again

    @property
    def is_open(self) -> bool:
        if self.open_until > 0:
            if time.time() >= self.open_until:
                logger.info("Circuit breaker cooldown expired, resuming")
                self.open_until = 0.0
                self.consecutive_failures = 0
                return False
            return True
        return False

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self) -> bool:
        """Record failure. Returns True if circuit breaker just tripped."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.open_until = time.time() + self.cooldown_seconds
            logger.error(
                "Circuit breaker OPEN after %d consecutive failures. "
                "Cooling down for %ds",
                self.consecutive_failures, self.cooldown_seconds,
            )
            return True
        return False


class PnLTracker:
    """Tracks realized PnL from closed rounds."""

    def __init__(self):
        self.realized_pnl = 0.0
        self.closed_rounds = 0
        self._rounds: dict[str, dict] = {}  # round_id -> entry info

    def record_entry(self, round_id: str, pair: str, spread: float,
                     tokens: float, spot_price: float):
        self._rounds[round_id] = {
            "pair": pair,
            "entry_spread": spread,
            "tokens": tokens,
            "spot_price": spot_price,
            "entry_time": time.time(),
        }

    def record_exit(self, round_id: str, exit_spread: float,
                    tokens_closed: float, spot_price: float,
                    duration_hours: float) -> float:
        entry = self._rounds.get(round_id)
        if not entry:
            return 0.0

        # PnL = (entry_spread - exit_spread) × tokens × spot × hold_years
        hold_years = duration_hours / 8760.0
        pnl = (entry["entry_spread"] - exit_spread) * tokens_closed * spot_price * hold_years
        self.realized_pnl += pnl
        self.closed_rounds += 1

        if tokens_closed >= entry["tokens"] - 0.1:
            del self._rounds[round_id]
        else:
            entry["tokens"] -= tokens_closed

        return pnl

    def calculate_unrealized(self, open_positions: list[dict]) -> tuple[float, list[dict]]:
        """
        Calculate unrealized PnL for open positions.
        Each item: {pair, mkt_a, mkt_b, pos_a, pos_b, bid_a, ask_a, bid_b, ask_b,
                     spot_price, ttm_years_a, ttm_years_b}
        Returns (total_upnl, details_list).

        PnL formula: (entry_rate - mark_rate) * tokens * time_to_maturity_years * spot_price
        Uses time-to-maturity (not holding time) because rates are APR applied over remaining life.
        """
        total = 0.0
        details = []
        for p in open_positions:
            pos_a = p['pos_a']
            pos_b = p['pos_b']
            side_a = pos_a['side']
            tokens = min(pos_a['tokens'], pos_b['tokens'])
            spot = p['spot_price']

            # Use entry_rate field if available (set at entry and recovery),
            # fall back to position_size / tokens for backward compatibility
            entry_rate_a = pos_a.get('entry_rate',
                                     pos_a.get('position_size', 0) / tokens if tokens > 0 else 0)
            entry_rate_b = pos_b.get('entry_rate',
                                     pos_b.get('position_size', 0) / tokens if tokens > 0 else 0)

            # Current mark rates (mid of bid/ask)
            mark_a = (p['bid_a'] + p['ask_a']) / 2
            mark_b = (p['bid_b'] + p['ask_b']) / 2

            # Entry spread and current spread
            if side_a == 1:  # Short A, Long B
                entry_spread = entry_rate_a - entry_rate_b
                current_spread = mark_a - mark_b
            else:  # Long A, Short B
                entry_spread = entry_rate_b - entry_rate_a
                current_spread = mark_b - mark_a

            entry_time = datetime.fromisoformat(pos_a['entry_time'])
            now = datetime.now(timezone.utc)
            hold_hours = (now - entry_time).total_seconds() / 3600

            # Time to maturity (years) — rates are APR applied over remaining life
            ttm_a = p.get('ttm_years_a', 0)
            ttm_b = p.get('ttm_years_b', 0)

            # uPnL per leg: (entry - mark) * tokens * TTM * spot for SHORT
            if side_a == 1:
                upnl_a = (entry_rate_a - mark_a) * tokens * spot * ttm_a
                upnl_b = (mark_b - entry_rate_b) * tokens * spot * ttm_b
            else:
                upnl_a = (mark_a - entry_rate_a) * tokens * spot * ttm_a
                upnl_b = (entry_rate_b - mark_b) * tokens * spot * ttm_b

            pair_upnl = upnl_a + upnl_b
            total += pair_upnl

            details.append({
                "pair": p['pair'],
                "mkt_a": p['mkt_a'], "mkt_b": p['mkt_b'],
                "side_a": "SHORT" if side_a == 1 else "LONG",
                "side_b": "LONG" if side_a == 1 else "SHORT",
                "tokens": round(tokens, 1),
                "entry_rate_a": entry_rate_a,
                "entry_rate_b": entry_rate_b,
                "mark_a": mark_a, "mark_b": mark_b,
                "upnl_a": round(upnl_a, 2),
                "upnl_b": round(upnl_b, 2),
                "pair_upnl": round(pair_upnl, 2),
                "hold_hours": round(hold_hours, 1),
                "entry_spread": round(entry_spread, 6),
                "current_spread": round(current_spread, 6),
            })

        return round(total, 2), details


class StrategyRunner:
    def __init__(self, context: IContext, interval_seconds: int = 60,
                 log_dir: str = "logs",
                 alert_handler: AlertHandler = None,
                 user_address: str = None,
                 summary_interval: int = SUMMARY_INTERVAL):
        self.context = context
        self.interval = interval_seconds
        self.strategies: list[BaseStrategy] = []
        self._running = False
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = None
        self._tick_count = 0
        self._circuit_breaker = CircuitBreaker()
        self._heartbeat_path = self.log_dir / "heartbeat.json"
        self._alert_handler = alert_handler
        self._user_address = user_address
        self._summary_interval = summary_interval
        self._start_time = 0.0
        self._tick_elapsed_history: list[float] = []
        self._pnl_tracker = PnLTracker()
        self._last_ifttt_summary_time = 0.0
        self._last_summary_time = 0.0
        self._ifttt_summary_interval = 6 * 3600  # 6 hours
        # 24h rolling stats
        self._stats_entries = 0
        self._stats_exits = 0
        self._stats_skips = defaultdict(int)  # reason -> count
        self._stats_max_spread = 0.0
        self._stats_max_spread_pair = ""

    def add_strategy(self, strategy: BaseStrategy):
        self.strategies.append(strategy)
        logger.info("Registered strategy: %s", strategy.name)

    def run(self):
        """Main loop. Ticks all registered strategies at the configured interval."""
        self._running = True
        self._start_time = time.time()
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Open JSONL log file (append mode, one file per session)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        bot_env = os.environ.get("BOT_ENV", "dev")
        log_path = self.log_dir / f"{bot_env}_{session_id}.jsonl"
        self._log_file = open(log_path, 'a')

        logger.info(
            "Starting with %d strategy(ies), interval=%ds, summary every %d ticks",
            len(self.strategies), self.interval, self._summary_interval,
        )
        logger.info("Logging to %s", log_path)

        try:
            while self._running:
                tick_start = time.time()
                now = datetime.now(timezone.utc)
                self._tick_count += 1

                # Circuit breaker check
                if self._circuit_breaker.is_open:
                    remaining = self._circuit_breaker.open_until - time.time()
                    logger.warning(
                        "Circuit breaker OPEN, skipping tick #%d (%.0fs remaining)",
                        self._tick_count, remaining,
                    )
                    self._write_heartbeat(now, status="circuit_breaker_open")
                    time.sleep(min(self.interval, 30))
                    continue

                logger.info("--- Tick #%d %s ---", self._tick_count, now.strftime('%H:%M:%S'))

                # Invalidate market cache each tick so we get fresh data
                self.context.data._market_cache.clear()

                tick_had_failure = False
                all_events = []

                for strategy in self.strategies:
                    try:
                        events = strategy.on_tick(self.context)
                        if events:
                            self._process_events(now, strategy.name, events)
                            self._track_stats(events)
                            all_events.extend(events)
                    except Exception as e:
                        tick_had_failure = True
                        logger.error(
                            "Error in %s: %s", strategy.name, e, exc_info=True,
                        )

                # Dispatch alerts
                if self._alert_handler and all_events:
                    try:
                        self._alert_handler.handle_events(all_events)
                    except Exception as e:
                        logger.warning("Alert handler error: %s", e)

                # Liquidation detection is now handled inside strategy.on_tick()
                # using the same collateral data already fetched that tick.
                # No separate API call needed here.

                # Circuit breaker tracking
                if tick_had_failure:
                    just_tripped = self._circuit_breaker.record_failure()
                    if just_tripped and self._alert_handler:
                        try:
                            self._alert_handler.handle_events([{
                                "type": "circuit_breaker",
                                "status": "open",
                                "consecutive_failures": self._circuit_breaker.consecutive_failures,
                                "cooldown_seconds": self._circuit_breaker.cooldown_seconds,
                            }])
                        except Exception as e:
                            logger.warning("CB alert error: %s", e)
                else:
                    self._circuit_breaker.record_success()

                elapsed = time.time() - tick_start
                self._tick_elapsed_history.append(elapsed)

                # Write tick_summary event
                self._write_tick_summary(now, elapsed, all_events)

                # Write heartbeat
                self._write_heartbeat(now, status="ok", elapsed=elapsed)

                # Periodic full summary — skip if nothing new, but force every 6h
                if self._tick_count % self._summary_interval == 0:
                    has_activity = any(
                        e.get('type') in ('entry', 'exit', 'exec_fail')
                        for e in all_events
                    )
                    hours_since_summary = (
                        (time.time() - self._last_summary_time) / 3600
                        if self._last_summary_time else float('inf')
                    )
                    if has_activity or hours_since_summary >= 6:
                        self._print_full_summary(now, all_events)
                        self._last_summary_time = time.time()

                logger.info("Tick completed in %.1fs", elapsed)
                sleep_time = max(0, self.interval - elapsed)
                if sleep_time > 0 and self._running:
                    time.sleep(sleep_time)
        finally:
            if self._log_file:
                self._log_file.close()
            logger.info("Stopped after %d ticks. Log: %s", self._tick_count, log_path)

    def _track_stats(self, events: list[dict]):
        """Update rolling stats from events."""
        for e in events:
            etype = e.get('type')
            if etype == 'entry':
                self._stats_entries += 1
                # Track in PnL tracker
                self._pnl_tracker.record_entry(
                    round_id=e.get('round_id', ''),
                    pair=e.get('pair', ''),
                    spread=e.get('spread', 0),
                    tokens=e.get('tokens', 0),
                    spot_price=e.get('spot_price', 1.0),
                )
            elif etype == 'exit':
                self._stats_exits += 1
                spot = 1.0  # TODO: get from event when available
                self._pnl_tracker.record_exit(
                    round_id=e.get('round_id', ''),
                    exit_spread=e.get('current_spread', 0),
                    tokens_closed=e.get('tokens_closed', 0),
                    spot_price=spot,
                    duration_hours=e.get('duration_hours', 0),
                )
            elif etype == 'skip':
                self._stats_skips[e.get('reason', 'unknown')] += 1
            elif etype == 'scan':
                # Track max spread
                bid_a = e.get('bid_a', 0)
                ask_b = e.get('ask_b', 0)
                bid_b = e.get('bid_b', 0)
                ask_a = e.get('ask_a', 0)
                spread = max(bid_a - ask_b, bid_b - ask_a)
                if spread > self._stats_max_spread:
                    self._stats_max_spread = spread
                    self._stats_max_spread_pair = e.get('pair', '')

    def _print_full_summary(self, now: datetime, current_events: list[dict]):
        """Print full summary to console and write to JSONL."""
        uptime_hours = (time.time() - self._start_time) / 3600
        avg_tick = (sum(self._tick_elapsed_history) / len(self._tick_elapsed_history)
                    if self._tick_elapsed_history else 0)

        # Gather open positions for unrealized PnL
        open_positions = self._gather_open_positions(current_events)
        unrealized_pnl, upnl_details = self._pnl_tracker.calculate_unrealized(open_positions)

        # Collateral info (detailed)
        collaterals = {}
        if self._user_address and hasattr(self.context.data, 'get_collateral_detail'):
            try:
                raw = self.context.data.get_collateral_detail(self._user_address)
                for tid, info in raw.items():
                    name = TOKEN_NAMES.get(tid, f"T{tid}")
                    collaterals[name] = info
                    collaterals[name]["token_id"] = tid
            except Exception:
                pass

        # Top spreads from current scan events
        scans = [e for e in current_events if e.get('type') == 'scan']
        spread_list = []
        for s in scans:
            bid_a, ask_b = s.get('bid_a', 0), s.get('ask_b', 0)
            bid_b, ask_a = s.get('bid_b', 0), s.get('ask_a', 0)
            sp = max(bid_a - ask_b, bid_b - ask_a)
            spread_list.append({"pair": s['pair'], "spread": sp * 100,
                                "holding": s.get('has_position', False)})
        spread_list.sort(key=lambda x: x['spread'], reverse=True)

        total_pnl = unrealized_pnl + self._pnl_tracker.realized_pnl

        # --- Console output ---
        sep = "=" * 78
        logger.info(sep)
        logger.info("FR Arb Summary (Tick #%d | %s UTC)", self._tick_count, now.strftime('%Y-%m-%d %H:%M:%S'))
        logger.info(sep)
        logger.info("Uptime: %.1fh | Avg Tick: %.0fs | Failures: %d",
                     uptime_hours, avg_tick, self._circuit_breaker.consecutive_failures)
        logger.info("")

        # Collateral
        if collaterals:
            logger.info("Collateral:")
            for name, info in collaterals.items():
                mr = info.get('margin_ratio', 0)
                mr_str = f"{mr:.1%}" if mr > 0 else "0.0%"
                logger.info("  %s: Net=%.6f | Avail=%.6f | IM=%.6f | MM=%.6f | MR=%s",
                             name, info.get('net_balance', 0), info['available'],
                             info.get('initial_margin', 0), info.get('maint_margin', 0), mr_str)
                for pos in info.get('positions', []):
                    side_str = "SHORT" if pos.get('side') == 1 else "LONG"
                    logger.info("    [%d] %s  size=%.4f  entry=%.4f%%  mark=%.4f%%  uPnL=%.6f",
                                 pos['market_id'], side_str, pos['size'],
                                 pos['entry_rate'] * 100, pos['mark_rate'] * 100,
                                 pos['unrealized_pnl'])
        logger.info("")

        # Positions
        logger.info("Pairs: %d active | Positions: %d", len(scans), len(upnl_details))
        if upnl_details:
            logger.info("")
            logger.info("Open Positions & Unrealized PnL:")
            for d in upnl_details:
                logger.info("  %s (%.1fh)", d['pair'], d['hold_hours'])
                logger.info("    Leg A [%d] %s  %.1f tokens @ %.2f%%  mark=%.2f%%  uPnL: $%+.2f",
                             d['mkt_a'], d['side_a'], d['tokens'],
                             d['entry_rate_a'] * 100, d['mark_a'] * 100, d['upnl_a'])
                logger.info("    Leg B [%d] %s  %.1f tokens @ %.2f%%  mark=%.2f%%  uPnL: $%+.2f",
                             d['mkt_b'], d['side_b'], d['tokens'],
                             d['entry_rate_b'] * 100, d['mark_b'] * 100, d['upnl_b'])
                logger.info("    Entry Spread: %.2f%%  Current: %.2f%%  Pair uPnL: $%+.2f",
                             d['entry_spread'] * 100, d['current_spread'] * 100, d['pair_upnl'])
        logger.info("")

        # PnL
        logger.info("PnL:")
        logger.info("  Unrealized:  $%+.2f  (%d pair%s)",
                     unrealized_pnl, len(upnl_details), "s" if len(upnl_details) != 1 else "")
        logger.info("  Realized:    $%+.2f  (%d round%s closed)",
                     self._pnl_tracker.realized_pnl, self._pnl_tracker.closed_rounds,
                     "s" if self._pnl_tracker.closed_rounds != 1 else "")
        logger.info("  Total:       $%+.2f", total_pnl)
        logger.info("")

        # Top spreads
        if spread_list:
            logger.info("Spread Leaderboard (Top 5):")
            for s in spread_list[:5]:
                flag = "  (holding)" if s['holding'] else ""
                logger.info("  %-20s spread=%.2f%%%s", s['pair'], s['spread'], flag)
        logger.info("")

        # Session stats
        skip_parts = [f"{c}x {r}" for r, c in self._stats_skips.items()] if self._stats_skips else ["none"]
        logger.info("Session Stats:")
        logger.info("  Entries: %d | Exits: %d | Skips: %s",
                     self._stats_entries, self._stats_exits, ", ".join(skip_parts))
        if self._stats_max_spread > 0:
            logger.info("  Max Spread Seen: %.2f%% (%s)",
                         self._stats_max_spread * 100, self._stats_max_spread_pair)
        logger.info(sep)

        # --- JSONL ---
        summary_record = {
            "ts": now.isoformat(),
            "tick": self._tick_count,
            "type": "periodic_summary",
            "uptime_hours": round(uptime_hours, 2),
            "avg_tick_seconds": round(avg_tick, 1),
            "cb_failures": self._circuit_breaker.consecutive_failures,
            "active_pairs": len(scans),
            "open_positions": len(upnl_details),
            "pnl": {
                "unrealized": unrealized_pnl,
                "realized": round(self._pnl_tracker.realized_pnl, 2),
                "total": round(total_pnl, 2),
                "open_pairs": len(upnl_details),
                "closed_rounds": self._pnl_tracker.closed_rounds,
            },
            "collaterals": {
                name: {
                    "net_balance": info.get('net_balance', 0),
                    "available": info['available'],
                    "initial_margin": info.get('initial_margin', 0),
                    "maint_margin": info.get('maint_margin', 0),
                    "margin_ratio": info.get('margin_ratio', 0),
                    "positions": info.get('positions', []),
                }
                for name, info in collaterals.items()
            },
            "top_spreads": spread_list[:5],
            "stats": {
                "entries": self._stats_entries,
                "exits": self._stats_exits,
                "skips": dict(self._stats_skips),
                "max_spread": round(self._stats_max_spread, 6),
                "max_spread_pair": self._stats_max_spread_pair,
            },
            "position_details": upnl_details,
        }
        self._log_file.write(json.dumps(summary_record, default=str) + '\n')
        self._log_file.flush()

        # --- IFTTT alerts (every 6 hours) ---
        now_ts = time.time()
        if (self._alert_handler
                and now_ts - self._last_ifttt_summary_time >= self._ifttt_summary_interval):
            try:
                self._alert_handler.send_summary_alerts(summary_record)
                self._last_ifttt_summary_time = now_ts
            except Exception as e:
                logger.warning("Summary alert error: %s", e)

    def _gather_open_positions(self, current_events: list[dict]) -> list[dict]:
        """Collect open position data for PnL calculation."""
        positions = []
        scan_map = {}
        for e in current_events:
            if e.get('type') == 'scan' and e.get('has_position'):
                scan_map[e['pair']] = e

        for strategy in self.strategies:
            all_pos = self.context.state.get_all_positions(strategy.name)
            # Group by round_id to find pairs
            by_round: dict[str, list] = defaultdict(list)
            for mid, pos in all_pos.items():
                rid = pos.get('round_id', '')
                by_round[rid].append((mid, pos))

            for rid, legs in by_round.items():
                if len(legs) != 2:
                    continue
                (mkt_a, pos_a), (mkt_b, pos_b) = legs

                # Find matching scan event for current prices
                pair_name = None
                for scan_pair, scan_e in scan_map.items():
                    if {scan_e['market_id_a'], scan_e['market_id_b']} == {mkt_a, mkt_b}:
                        pair_name = scan_pair
                        break

                if not pair_name or pair_name not in scan_map:
                    continue

                scan = scan_map[pair_name]
                # Ensure mkt_a/mkt_b match scan ordering
                if mkt_a == scan['market_id_a']:
                    pass
                else:
                    mkt_a, mkt_b = mkt_b, mkt_a
                    pos_a, pos_b = pos_b, pos_a

                spot = self.context.data.get_spot_price(mkt_a) or 1.0
                now_ts = time.time()
                mat_a = self.context.data.get_maturity(mkt_a)
                mat_b = self.context.data.get_maturity(mkt_b)
                ttm_a = max(0.0, (mat_a - now_ts) / 31_536_000) if mat_a else 0.0
                ttm_b = max(0.0, (mat_b - now_ts) / 31_536_000) if mat_b else 0.0
                positions.append({
                    "pair": pair_name,
                    "mkt_a": mkt_a, "mkt_b": mkt_b,
                    "pos_a": pos_a, "pos_b": pos_b,
                    "bid_a": scan['bid_a'], "ask_a": scan['ask_a'],
                    "bid_b": scan['bid_b'], "ask_b": scan['ask_b'],
                    "spot_price": spot,
                    "ttm_years_a": ttm_a,
                    "ttm_years_b": ttm_b,
                })

        return positions

    def _process_events(self, tick_time: datetime, strategy_name: str, events: list[dict]):
        """Write events to JSONL and print summary to console."""
        entries = [e for e in events if e.get('type') == 'entry']
        exits = [e for e in events if e.get('type') == 'exit']
        holds = [e for e in events if e.get('type') == 'hold']
        exec_fails = [e for e in events if e.get('type') == 'exec_fail']
        skips = [e for e in events if e.get('type') == 'skip']
        scans = [e for e in events if e.get('type') == 'scan']

        # Holding = hold events + exec_fail (position still open after failed close)
        holding_count = len(holds) + len(exec_fails)
        n_signal = len(entries) + len(skips)
        logger.info(
            "[%s] Scanned %d markets | Signals: %d | Entries: %d | Exits: %d | Holding: %d",
            strategy_name, len(scans), n_signal, len(entries), len(exits), holding_count,
        )

        for e in entries:
            label = e.get('pair') or e.get('symbol', str(e.get('market_id', '?')))
            if 'spread' in e:
                logger.info("  ENTRY %s | spread=%.2f%% | %.1f tokens",
                            label, e['spread'] * 100, e.get('tokens', 0))
            else:
                logger.info("  ENTRY [%s] | u=%+.4f | $%s",
                            label, e.get('funding_rate', 0), e.get('position_size', 0))
        for e in exits:
            label = e.get('pair') or e.get('symbol', str(e.get('market_id', '?')))
            if 'current_spread' in e:
                logger.info("  EXIT  %s | spread=%.2f%% | held %.1fh | %s",
                            label, e['current_spread'] * 100,
                            e.get('duration_hours', 0), e.get('reason', ''))
            else:
                logger.info("  EXIT  [%s] | u=%+.4f | held %.1fh | PnL=$%.2f",
                            label, e.get('funding_rate', 0),
                            e.get('duration_hours', 0), e.get('pnl', 0))
        for e in holds:
            label = e.get('pair') or e.get('symbol', str(e.get('market_id', '?')))
            if 'current_spread' in e:
                logger.info("  HOLD  %s | spread=%.2f%% | %.1fh",
                            label, e['current_spread'] * 100, e.get('duration_hours', 0))
            else:
                logger.info("  HOLD  [%s] | u=%+.4f | entry=%+.4f | %.1fh | PnL=$%.2f",
                            label, e.get('funding_rate', 0),
                            e.get('entry_rate', 0), e.get('duration_hours', 0), e.get('pnl', 0))
        for e in exec_fails:
            label = e.get('pair') or str(e.get('market_id_a', '?'))
            logger.info("  FAIL  %s | %s | spread=%.2f%%",
                        label, e.get('reason', ''), e.get('spread', 0) * 100)

        # Write ALL events to JSONL
        ts = tick_time.isoformat()
        for event in events:
            record = {
                "ts": ts,
                "tick": self._tick_count,
                "strategy": strategy_name,
                **event,
            }
            line = json.dumps(record, default=str)
            self._log_file.write(line + '\n')
        self._log_file.flush()

    def _write_tick_summary(self, tick_time: datetime, elapsed: float, events: list[dict]):
        """Write a tick_summary event to JSONL for monitoring."""
        summary = {
            "ts": tick_time.isoformat(),
            "tick": self._tick_count,
            "type": "tick_summary",
            "elapsed_seconds": round(elapsed, 3),
            "total_events": len(events),
            "entries": sum(1 for e in events if e.get('type') == 'entry'),
            "exits": sum(1 for e in events if e.get('type') == 'exit'),
            "holds": sum(1 for e in events if e.get('type') == 'hold'),
            "skips": sum(1 for e in events if e.get('type') == 'skip'),
            "scans": sum(1 for e in events if e.get('type') == 'scan'),
            "cb_failures": self._circuit_breaker.consecutive_failures,
        }
        self._log_file.write(json.dumps(summary) + '\n')
        self._log_file.flush()

    def _write_heartbeat(self, now: datetime, status: str = "ok", elapsed: float = 0.0):
        """Write heartbeat.json for external monitoring."""
        heartbeat = {
            "last_tick": now.isoformat(),
            "tick_count": self._tick_count,
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
            "strategies": [s.name for s in self.strategies],
        }
        try:
            self._heartbeat_path.write_text(json.dumps(heartbeat, indent=2))
        except Exception as e:
            logger.warning("Failed to write heartbeat: %s", e)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received. Stopping...")
        self._running = False
