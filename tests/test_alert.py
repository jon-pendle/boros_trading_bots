"""Tests for AlertHandler and IFTTTAlert."""
import pytest
from unittest.mock import patch, MagicMock
from strategies.framework.alert import IFTTTAlert, AlertHandler


class TestIFTTTAlert:
    def test_disabled_without_key(self):
        alert = IFTTTAlert("")
        assert alert.enabled is False
        assert alert.send({"action": "test"}) is False

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
        assert alert.send({"entry": "[58] SOL", "funding": "-15.90%"}) is True
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["entry"] == "[58] SOL"

    @patch("strategies.framework.alert.requests.post")
    def test_send_failure(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)
        alert = IFTTTAlert("key")
        assert alert.send({"action": "test"}) is False

    @patch("strategies.framework.alert.requests.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = Exception("connection error")
        alert = IFTTTAlert("key")
        assert alert.send({"action": "test"}) is False


class TestAlertHandler:
    def _make_handler(self):
        ifttt = MagicMock()
        ifttt.send.return_value = True
        return AlertHandler(ifttt), ifttt

    def test_entry_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "entry", "market_id": 58,
            "symbol": "Hyperliquid SOL", "funding_rate": -0.15,
            "position_size": 1000, "tokens": 100,
            "best_bid": -0.04, "best_ask": -0.03,
            "spot_price": 83.0,
        }])
        ifttt.send.assert_called_once()
        payload = ifttt.send.call_args[0][0]
        assert "entry" in payload
        assert "58" in payload["entry"]

    def test_exit_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "exit", "market_id": 58,
            "symbol": "Hyperliquid SOL", "funding_rate": 0.01,
            "entry_rate": -0.15, "duration_hours": 14.5,
            "position_size": 1000,
        }])
        ifttt.send.assert_called_once()
        assert "exit" in ifttt.send.call_args[0][0]

    def test_exit_failed_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "exit_failed", "market_id": 40,
            "symbol": "OKX ETHUSDT", "funding_rate": 0.01,
            "entry_rate": -0.06, "duration_hours": 20.0,
        }])
        ifttt.send.assert_called_once()
        assert "exit_failed" in ifttt.send.call_args[0][0]

    def test_prolonged_hold_triggers_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "hold", "market_id": 71, "prolonged": True,
            "symbol": "Binance XRPUSDT", "funding_rate": -0.10,
            "entry_rate": -0.08, "duration_hours": 50.0,
        }])
        ifttt.send.assert_called_once()
        assert "prolonged_hold" in ifttt.send.call_args[0][0]

    def test_prolonged_hold_alerts_only_once(self):
        handler, ifttt = self._make_handler()
        event = {
            "type": "hold", "market_id": 71, "prolonged": True,
            "symbol": "Binance XRPUSDT", "funding_rate": -0.10,
            "entry_rate": -0.08, "duration_hours": 50.0,
        }
        handler.handle_events([event])
        handler.handle_events([event])
        handler.handle_events([event])
        assert ifttt.send.call_count == 1

    def test_prolonged_resets_after_exit(self):
        handler, ifttt = self._make_handler()
        hold_event = {
            "type": "hold", "market_id": 71, "prolonged": True,
            "symbol": "Binance XRPUSDT", "funding_rate": -0.10,
            "entry_rate": -0.08, "duration_hours": 50.0,
        }
        exit_event = {
            "type": "exit", "market_id": 71,
            "symbol": "Binance XRPUSDT", "funding_rate": 0.01,
            "entry_rate": -0.08, "duration_hours": 55.0,
            "position_size": 1000,
        }
        handler.handle_events([hold_event])
        handler.handle_events([exit_event])
        handler.handle_events([hold_event])
        assert ifttt.send.call_count == 3

    def test_scan_no_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "scan", "market_id": 23,
            "symbol": "BTC", "funding_rate": 0.01,
        }])
        ifttt.send.assert_not_called()

    def test_skip_no_alert(self):
        handler, ifttt = self._make_handler()
        handler.handle_events([{
            "type": "skip", "market_id": 23,
            "symbol": "BTC", "funding_rate": -0.08,
            "reason": "max_positions",
        }])
        ifttt.send.assert_not_called()
