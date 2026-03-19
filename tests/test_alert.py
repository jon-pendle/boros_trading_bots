"""Tests for AlertHandler and IFTTTAlert."""
import pytest
from unittest.mock import patch, MagicMock
from strategies.framework.alert import IFTTTAlert, AlertHandler


def _get_msg(ifttt_mock, call_idx=0):
    """Extract the message string from IFTTT send call.
    New format: {"Env[Px]": "message"} — value is the message."""
    payload = ifttt_mock.send.call_args_list[call_idx][0][0]
    # Return the first value in the dict (the message)
    return list(payload.values())[0]


def _get_key(ifttt_mock, call_idx=0):
    """Extract the JSON key from IFTTT send call (e.g. 'Dev[P1]')."""
    payload = ifttt_mock.send.call_args_list[call_idx][0][0]
    return list(payload.keys())[0]


class TestIFTTTAlert:
    def test_disabled_without_key(self):
        alert = IFTTTAlert("")
        assert alert.enabled is False
        assert alert.send({"msg": "test"}) is False

    def test_enabled_with_key(self):
        alert = IFTTTAlert("test_key_123")
        assert alert.enabled is True
        assert "boros_event" in alert.webhook_url

    def test_custom_event_name(self):
        alert = IFTTTAlert("key", event_name="my_event")
        assert "my_event" in alert.webhook_url

    @patch("strategies.framework.alert.requests.post")
    def test_send_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        alert = IFTTTAlert("key")
        assert alert.send({"msg": "ENTRY BTC_23_60"}) is True
        mock_post.assert_called_once()

    @patch("strategies.framework.alert.requests.post")
    def test_send_failure(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)
        alert = IFTTTAlert("key")
        assert alert.send({"msg": "test"}) is False

    @patch("strategies.framework.alert.requests.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = Exception("connection error")
        alert = IFTTTAlert("key")
        assert alert.send({"msg": "test"}) is False


class TestAlertHandler:
    def _make_handler(self):
        ifttt = MagicMock()
        ifttt.send.return_value = True
        return AlertHandler(ifttt), ifttt

    def test_entry_pair_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "entry", "pair": "BTC_20260327_23_60",
            "market_id_a": 23, "market_id_b": 60,
            "scenario": 1, "spread": 0.045, "tokens": 50.0,
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "ENTRY" in msg
        assert "BTC_20260327_23_60" in msg
        assert "P1" in _get_key(ifttt)

    def test_entry_single_market_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "entry", "symbol": "SOL",
            "funding_rate": -0.15, "position_size": 1000,
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "ENTRY" in msg

    def test_exit_pair_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "exit", "pair": "HYPE_20260327_67_68",
            "current_spread": 0.03, "duration_hours": 5.2,
            "reason": "Spread=3.00%",
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "EXIT" in msg

    def test_exit_with_spread_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "exit", "pair": "SOL_58_59",
            "current_spread": 0.02, "duration_hours": 14.5,
            "tokens_closed": 10.0, "reason": "Spread=2.00%",
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "EXIT" in msg

    def test_liquidation_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "liquidation", "market_id": 58,
            "symbol": "SOL", "position_size": 1000,
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "LIQUIDATED" in msg
        assert "P0" in _get_key(ifttt)

    def test_scan_no_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "scan", "pair": "BTC_23_60",
            "bid_a": 0.05, "ask_b": 0.03,
        }])
        ifttt.send.assert_not_called()

    def test_skip_accumulated_not_immediate(self):
        """Skip events are accumulated, not sent immediately (rolled into P3)."""
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "skip", "pair": "BTC_23_60",
            "spread": 0.045, "reason": "insufficient_margin",
        }])
        ifttt.send.assert_not_called()
        assert len(handler._pending_skips) == 1

    def test_skip_included_in_summary(self):
        """Accumulated skips are cleared after summary send."""
        handler, ifttt = self._make_handler()
        handler.handle_events([
            {"type": "skip", "pair": "BTC_23_60", "spread": 0.045, "reason": "insufficient_margin"},
            {"type": "skip", "pair": "BTC_23_60", "spread": 0.044, "reason": "insufficient_margin"},
            {"type": "skip", "pair": "SOL_58_59", "spread": 0.05, "reason": "onchain_position_exists"},
        ])
        summary = {
            "tick": 10, "uptime_hours": 0.5, "avg_tick_seconds": 30,
            "cb_failures": 0, "pnl": {"unrealized": 0, "realized": 0, "total": 0,
                                       "open_pairs": 0, "closed_rounds": 0},
            "stats": {"entries": 0, "exits": 0, "skips": {}, "max_spread": 0, "max_spread_pair": ""},
        }
        handler.send_summary_alerts(summary)
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "P3" in _get_key(ifttt)
        # Pending skips cleared after summary
        assert len(handler._pending_skips) == 0

    def test_exec_fail_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "exec_fail", "pair": "BTC_60_61",
            "reason": "leg_b_failed_rollback",
            "spread": 0.045, "tokens": 10.0,
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "FAIL" in msg
        assert "leg_b_failed_rollback" in msg

    def test_exec_fail_throttled_per_pair(self):
        """Same pair exec_fail within 30min window is suppressed."""
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "exec_fail", "pair": "BTC_60_61",
            "reason": "dual_entry_failed", "spread": 0.045, "tokens": 10.0,
        }])
        assert ifttt.send.call_count == 1
        # Second call within throttle window — suppressed
        handler.handle_events([{
            "type": "exec_fail", "pair": "BTC_60_61",
            "reason": "dual_entry_failed", "spread": 0.046, "tokens": 10.0,
        }])
        assert ifttt.send.call_count == 1  # still 1
        # Different pair — not throttled
        handler.handle_events([{
            "type": "exec_fail", "pair": "SOL_58_59",
            "reason": "dual_entry_failed", "spread": 0.05, "tokens": 5.0,
        }])
        assert ifttt.send.call_count == 2

    def test_circuit_breaker_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "circuit_breaker", "status": "open",
            "consecutive_failures": 5, "cooldown_seconds": 300,
        }])
        ifttt.send.assert_called_once()
        msg = _get_msg(ifttt)
        assert "CB_OPEN" in msg
        assert "P0" in _get_key(ifttt)

    def test_summary_alerts_sent(self):
        handler, ifttt = self._make_handler()
        summary = {
            "tick": 10,
            "uptime_hours": 0.5,
            "avg_tick_seconds": 30,
            "cb_failures": 0,
            "active_pairs": 14,
            "pnl": {
                "unrealized": 5.34,
                "realized": 12.80,
                "total": 18.14,
                "open_pairs": 1,
                "closed_rounds": 2,
            },
            "collaterals": {"USDT": {"available": 1000.0}},
            "top_spreads": [
                {"pair": "HYPE_67_68", "spread": 2.1},
            ],
            "stats": {
                "entries": 1, "exits": 0,
                "skips": {"insufficient_margin": 3},
                "max_spread": 0.028, "max_spread_pair": "BTC_60_61",
            },
        }
        handler.send_summary_alerts(summary)
        # Single consolidated message
        ifttt.send.assert_called_once()
        key = _get_key(ifttt)
        msg = _get_msg(ifttt)
        assert "P3" in key
        assert "PnL" in msg
