"""Tests for on-chain state sync — positions missing on-chain get cleared."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from strategies.fr_arb.strategy import FRArbitrageStrategy
from strategies.fr_arb import config as fr_config
from strategies.framework.state_manager import InMemoryStateManager
from strategies.framework.alert import AlertHandler, IFTTTAlert


def make_context(now=None):
    ctx = MagicMock()
    ctx.now = now or datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ctx.state = InMemoryStateManager()
    ctx.executor = MagicMock()
    ctx.executor.submit_dual_order.return_value = True
    ctx.executor.close_dual_position.return_value = {"status": "dry_run"}
    ctx.data = MagicMock()
    ctx.data._market_cache = {}
    return ctx


def setup_with_positions(ctx, market_id=58, side=1, tokens=10.0):
    """Set up a local state position and configure mocks."""
    entry_time = ctx.now - timedelta(hours=5)
    ctx.state.set_position("fr_arb", market_id, {
        "entry_time": entry_time.isoformat(),
        "side": side,
        "position_size": 0,
        "tokens": tokens,
        "round_id": f"test_{market_id}",
    })
    # No pairs generated — so pair loop does nothing
    ctx.data.get_all_market_ids.return_value = []
    ctx.data.generate_pairs.return_value = {}


class TestOnchainStateSync:
    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_no_clear_when_on_chain(self):
        """Position exists both locally and on-chain → state preserved."""
        ctx = make_context()
        setup_with_positions(ctx, market_id=58)

        ctx.data.get_collateral_detail.return_value = {
            3: {
                "available": 100.0,
                "positions": [{"market_id": 58, "size": 10.0, "size_wei": "10000000000000000000",
                               "side": 1, "entry_rate": 0.05, "mark_rate": 0.04,
                               "unrealized_pnl": 0, "token_id": 3}],
            },
        }

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        assert ctx.state.get_position("fr_arb", 58) is not None

    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_clear_when_missing_on_chain(self):
        """Position in state but not on-chain → state cleared."""
        ctx = make_context()
        setup_with_positions(ctx, market_id=58)

        ctx.data.get_collateral_detail.return_value = {
            3: {"available": 100.0, "positions": []},
        }

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        assert ctx.state.get_position("fr_arb", 58) is None

    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_no_clear_when_collateral_api_fails(self):
        """Collateral API fails → state untouched."""
        ctx = make_context()
        setup_with_positions(ctx, market_id=58)

        ctx.data.get_collateral_detail.side_effect = Exception("timeout")

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        assert ctx.state.get_position("fr_arb", 58) is not None

    @patch.object(fr_config, "USER_ADDRESS", "")
    def test_no_clear_without_user_address(self):
        """No USER_ADDRESS → no collateral fetch, state untouched."""
        ctx = make_context()
        setup_with_positions(ctx, market_id=58)

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        assert ctx.state.get_position("fr_arb", 58) is not None

    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_partial_clear(self):
        """Two positions in state, only one missing on-chain → only one cleared."""
        ctx = make_context()
        entry_time = ctx.now - timedelta(hours=5)
        ctx.state.set_position("fr_arb", 58, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 0, "tokens": 10.0, "round_id": "r1",
        })
        ctx.state.set_position("fr_arb", 59, {
            "entry_time": entry_time.isoformat(),
            "side": 0, "position_size": 0, "tokens": 10.0, "round_id": "r1",
        })
        ctx.data.get_all_market_ids.return_value = []
        ctx.data.generate_pairs.return_value = {}

        ctx.data.get_collateral_detail.return_value = {
            3: {
                "available": 100.0,
                "positions": [{"market_id": 59, "size": 10.0, "size_wei": "10000000000000000000",
                               "side": 0, "entry_rate": 0.03, "mark_rate": 0.03,
                               "unrealized_pnl": 0, "token_id": 3}],
            },
        }

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        assert ctx.state.get_position("fr_arb", 58) is None
        assert ctx.state.get_position("fr_arb", 59) is not None

    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_onchain_position_added_to_state(self):
        """Position on-chain but not in state → added to state."""
        ctx = make_context()
        ctx.data.get_all_market_ids.return_value = []
        ctx.data.generate_pairs.return_value = {}

        ctx.data.get_collateral_detail.return_value = {
            3: {
                "available": 100.0,
                "positions": [{"market_id": 58, "size": 5.0, "size_wei": "5000000000000000000",
                               "side": 1, "entry_rate": 0.05, "mark_rate": 0.04,
                               "unrealized_pnl": 0, "token_id": 3}],
            },
        }

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        pos = ctx.state.get_position("fr_arb", 58)
        assert pos is not None
        assert pos["tokens"] == 5.0
        assert pos["side"] == 1

    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_size_synced_from_onchain(self):
        """Position size in state differs from on-chain → synced."""
        ctx = make_context()
        setup_with_positions(ctx, market_id=58, tokens=10.0)

        ctx.data.get_collateral_detail.return_value = {
            3: {
                "available": 100.0,
                "positions": [{"market_id": 58, "size": 8.0, "size_wei": "8000000000000000000",
                               "side": 1, "entry_rate": 0.05, "mark_rate": 0.04,
                               "unrealized_pnl": 0, "token_id": 3}],
            },
        }

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        pos = ctx.state.get_position("fr_arb", 58)
        assert pos["tokens"] == 8.0
        assert pos["tokens_wei"] == "8000000000000000000"
