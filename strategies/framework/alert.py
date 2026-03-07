"""
Alert module - dispatches strategy events to notification channels.

Supports IFTTT Webhooks (JSON format) for push notifications.
IFTTT setup:
  1. Enable Webhooks service, create applet: IF Webhook(boros_event) THEN notification
  2. Set env: IFTTT_WEBHOOK_KEY=your_key
"""
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


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

    def send(self, payload: dict) -> bool:
        if not self.enabled:
            return False
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                logger.info("IFTTT alert sent: %s", next(iter(payload.values()), ""))
                return True
            else:
                logger.warning("IFTTT alert failed: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning("IFTTT alert error: %s", e)
        return False


class AlertHandler:
    """Processes strategy events and dispatches alerts."""

    def __init__(self, ifttt: IFTTTAlert):
        self.ifttt = ifttt
        self._prolonged_alerted: set[int] = set()

    def handle_events(self, events: list[dict]):
        for event in events:
            event_type = event.get("type")

            if event_type == "entry":
                self._on_entry(event)
            elif event_type == "exit":
                self._on_exit(event)
                self._prolonged_alerted.discard(event.get("market_id"))
            elif event_type == "exit_failed":
                self._on_exit_failed(event)
            elif event_type == "hold" and event.get("prolonged"):
                self._on_prolonged_hold(event)

    def _fmt_market(self, e: dict) -> str:
        return f"[{e['market_id']}] {e['symbol']}"

    def _on_entry(self, e: dict):
        self.ifttt.send({
            "entry": self._fmt_market(e),
            "funding": f"{e['funding_rate']:+.2%}",
            "size_usd": e["position_size"],
            "tokens": round(e["tokens"], 4),
            "bid_ask": f"{e.get('best_bid', 0):+.4f}/{e.get('best_ask', 0):+.4f}",
            "spot": e.get("spot_price"),
        })

    def _on_exit(self, e: dict):
        self.ifttt.send({
            "exit": self._fmt_market(e),
            "funding": f"{e['funding_rate']:+.2%}",
            "entry_rate": f"{e['entry_rate']:+.2%}",
            "held_hours": e["duration_hours"],
            "size_usd": e.get("position_size"),
        })

    def _on_exit_failed(self, e: dict):
        self.ifttt.send({
            "exit_failed": self._fmt_market(e),
            "funding": f"{e['funding_rate']:+.2%}",
            "held_hours": e["duration_hours"],
            "note": "manual intervention needed",
        })

    def _on_prolonged_hold(self, e: dict):
        market_id = e.get("market_id")
        if market_id in self._prolonged_alerted:
            return
        self._prolonged_alerted.add(market_id)

        self.ifttt.send({
            "prolonged_hold": self._fmt_market(e),
            "funding": f"{e['funding_rate']:+.2%}",
            "entry_rate": f"{e['entry_rate']:+.2%}",
            "held_hours": e["duration_hours"],
        })
