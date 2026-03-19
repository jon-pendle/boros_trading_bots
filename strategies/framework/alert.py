"""
Alert module - dispatches strategy events to notification channels.

Alert tiers:
  P0 (critical):  liquidation, circuit_breaker_open  → immediate, always
  P1 (action):    entry, exit                        → immediate, always
  P1 (action):    exec_fail                          → throttled (1 per pair per 30min)
  P2 (signal):    skip                               → suppressed (rolled into P3 summary)
  P3 (periodic):  summary                            → 1 consolidated message per cycle
  ---             scan, hold, tick_summary            → no alert (too frequent)

IFTTT setup:
  1. Enable Webhooks service, create applet: IF Webhook(boros_event) THEN notification
  2. Set env: IFTTT_WEBHOOK_KEY=your_key
"""
import logging
import os
import time
import requests

logger = logging.getLogger(__name__)

# Throttle window for exec_fail alerts (seconds)
EXEC_FAIL_THROTTLE_SECONDS = 1800  # 30 minutes


class IFTTTAlert:
    """Sends alerts via IFTTT Webhooks with clean JSON payload."""

    def __init__(self, webhook_key: str, event_name: str = "boros_event",
                 timeout: int = 5):
        self.webhook_key = webhook_key
        self.event_name = event_name
        self.timeout = timeout
        self.enabled = bool(webhook_key)
        if not self.enabled:
            logger.warning("IFTTT alert disabled (no IFTTT_WEBHOOK_KEY set)")

    @property
    def webhook_url(self) -> str:
        return f"https://maker.ifttt.com/trigger/{self.event_name}/json/with/key/{self.webhook_key}"

    def send(self, payload: dict, retries: int = 2) -> bool:
        if not self.enabled:
            return False
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    logger.debug("IFTTT alert sent")
                    return True
                else:
                    logger.warning("IFTTT alert failed: HTTP %d", resp.status_code)
            except Exception as e:
                logger.warning("IFTTT alert error (attempt %d/%d): %s", attempt, retries, e)
        return False


def _tag(env: str, priority: str) -> str:
    """Build IFTTT JSON key like 'Prod[P1]' or 'Test[P3]'."""
    return f"{env}[{priority}]"


class AlertHandler:
    """Processes strategy events and dispatches alerts by priority tier."""

    def __init__(self, ifttt: IFTTTAlert):
        self.ifttt = ifttt
        self._env = os.environ.get("BOT_ENV", "dev").capitalize()
        # exec_fail throttle: {pair -> last_alert_timestamp}
        self._exec_fail_last: dict[str, float] = {}
        # P2 skip accumulator: collected between summary cycles
        self._pending_skips: list[dict] = []

    def handle_events(self, events: list[dict]):
        for event in events:
            event_type = event.get("type")

            # P0 — critical (always immediate)
            if event_type == "liquidation":
                self._on_liquidation(event)
            elif event_type == "circuit_breaker":
                self._on_circuit_breaker(event)
            # P1 — action (always immediate)
            elif event_type == "entry":
                self._on_entry(event)
            elif event_type == "exit":
                self._on_exit(event)
            # P1 — exec_fail (throttled per pair)
            elif event_type == "exec_fail":
                self._on_exec_fail(event)
            # P2 — skip (accumulate, send in summary)
            elif event_type == "skip":
                self._pending_skips.append(event)

    # ------------------------------------------------------------------
    # P3 — periodic summary
    # ------------------------------------------------------------------

    def send_summary_alerts(self, summary: dict):
        """Send periodic summary as a single compact IFTTT message."""
        pnl = summary.get('pnl', {})
        stats = summary.get('stats', {})

        # Collateral: only show non-zero
        collaterals = summary.get('collaterals', {})
        coll_parts = []
        for name, info in collaterals.items():
            net = info.get('net_balance', 0)
            if net > 0:
                mr = info.get('margin_ratio', 0)
                mr_str = f" MR:{mr:.0%}" if mr > 0 else ""
                coll_parts.append(f"{name}:{info['available']:.2f}/{net:.2f}{mr_str}")

        # Top 3 spreads
        top_spreads = summary.get('top_spreads', [])
        sp_parts = [f"{s['pair']}:{s['spread']:.1f}%" for s in top_spreads[:3]]

        self._pending_skips.clear()
        self.ifttt.send({
            _tag(self._env, "P3"): (
                f"T#{summary['tick']} {summary['uptime_hours']:.0f}h "
                f"P:{summary.get('active_pairs', 0)} "
                f"PnL:${pnl.get('total', 0):+.0f}"
                f"({pnl.get('open_pairs', 0)}o/{pnl.get('closed_rounds', 0)}c) "
                f"{' | '.join(coll_parts) if coll_parts else 'NoColl'} "
                f"{' | '.join(sp_parts)}"
                f" Max:{stats.get('max_spread', 0) * 100:.1f}%"
            ),
        })

    # ------------------------------------------------------------------
    # P0 — critical
    # ------------------------------------------------------------------

    def _on_liquidation(self, e: dict):
        self.ifttt.send({
            _tag(self._env, "P0"): (
                f"LIQUIDATED [{e.get('market_id', '?')}] "
                f"${e.get('position_size', 0)}"
            ),
        })

    def _on_circuit_breaker(self, e: dict):
        status = e.get('status', 'open')
        if status == 'open':
            self.ifttt.send({
                _tag(self._env, "P0"): (
                    f"CB_OPEN {e.get('consecutive_failures', 0)} fails "
                    f"cooldown {e.get('cooldown_seconds', 0)}s"
                ),
            })
        else:
            self.ifttt.send({
                _tag(self._env, "P0"): "CB_RECOVERED",
            })

    # ------------------------------------------------------------------
    # P1 — action taken
    # ------------------------------------------------------------------

    def _on_entry(self, e: dict):
        self.ifttt.send({
            _tag(self._env, "P1"): (
                f"ENTRY {e.get('pair', '?')} "
                f"sp={e.get('spread', 0):.1%} "
                f"{e.get('tokens', 0):.1f}tk "
                f"${e.get('size_usd_a', 0):.0f}+${e.get('size_usd_b', 0):.0f}"
            ),
        })

    def _on_exit(self, e: dict):
        self.ifttt.send({
            _tag(self._env, "P1"): (
                f"EXIT {e.get('pair', '?')} "
                f"sp={e.get('current_spread', 0):.1%} "
                f"{e.get('duration_hours', 0):.1f}h "
                f"{e.get('tokens_closed', 0):.1f}tk "
                f"{e.get('reason', '')}"
            ),
        })

    def _on_exec_fail(self, e: dict):
        """Throttled: max 1 alert per pair per 30 minutes."""
        pair = e.get('pair', '?')
        now = time.time()
        last = self._exec_fail_last.get(pair, 0)
        if now - last < EXEC_FAIL_THROTTLE_SECONDS:
            logger.debug("exec_fail alert throttled for %s (%.0fs since last)",
                         pair, now - last)
            return
        self._exec_fail_last[pair] = now
        self.ifttt.send({
            _tag(self._env, "P1"): (
                f"FAIL {pair} "
                f"{e.get('reason', '?')} "
                f"sp={e.get('spread', 0):.1%} "
                f"{e.get('tokens', 0):.1f}tk"
            ),
        })
