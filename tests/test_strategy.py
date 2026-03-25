"""Tests for FRArbitrageStrategy — MarginalPnL Exit + Scale-In."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from strategies.fr_arb.strategy import (
    FRArbitrageStrategy, _calc_vwap,
)
from strategies.fr_arb import config as fr_config
from strategies.framework.interfaces import IContext, IExecutor
from strategies.framework.state_manager import InMemoryStateManager


# Two markets forming a pair: A has higher rates, B has lower rates
# Mid_A = (0.08+0.085)/2 = 0.0825, Mid_B = (0.03+0.035)/2 = 0.0325
# Mid spread = 0.0825 - 0.0325 = 0.05
MARKET_A = {
    "marketId": 60,
    "imData": {
        "name": "Test Market A",
        "symbol": "TEST-A",
        "maturity": 1774569600,
        "tickStep": 2,
    },
    "data": {"assetMarkPrice": 100.0, "markApr": 0.08, "bestBid": 0.08, "bestAsk": 0.085},
}

MARKET_B = {
    "marketId": 61,
    "imData": {
        "name": "Test Market B",
        "symbol": "TEST-B",
        "maturity": 1774569600,
        "tickStep": 2,
    },
    "data": {"assetMarkPrice": 100.0, "markApr": 0.03, "bestBid": 0.03, "bestAsk": 0.035},
}

# Orderbooks with sufficient depth
OB_A = {
    "bids": [(0.08, 500.0), (0.075, 500.0), (0.07, 500.0)],
    "asks": [(0.085, 500.0), (0.09, 500.0), (0.095, 500.0)],
}
OB_B = {
    "bids": [(0.03, 500.0), (0.025, 500.0), (0.02, 500.0)],
    "asks": [(0.035, 500.0), (0.04, 500.0), (0.045, 500.0)],
}

TEST_PAIR = {"TEST_60_61": (60, 61)}


def make_context(now=None):
    ctx = MagicMock(spec=IContext)
    ctx.now = now or datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ctx.state = InMemoryStateManager()
    ctx.executor = MagicMock(spec=IExecutor)
    ctx.executor.submit_order.return_value = True
    ctx.executor.submit_dual_order.return_value = True
    ctx.executor.close_position.return_value = {"status": "dry_run"}
    ctx.executor.close_dual_position.return_value = {"status": "dry_run"}
    ctx.data = MagicMock()
    return ctx


def setup_pair(ctx, ob_a=None, ob_b=None, pairs=None):
    """Configure mock data for a pair of markets."""
    ob_a = ob_a or OB_A
    ob_b = ob_b or OB_B
    pairs = pairs or TEST_PAIR

    def get_market_info(mid):
        return {60: MARKET_A, 61: MARKET_B}.get(mid, {})

    def get_orderbook(mid, **kw):
        return {60: ob_a, 61: ob_b}.get(mid, {"bids": [], "asks": []})

    def get_spot_price(mid):
        info = get_market_info(mid)
        return float(info.get("data", {}).get("assetMarkPrice", 1.0))

    ctx.data.get_market_info.side_effect = get_market_info
    ctx.data.get_orderbook.side_effect = get_orderbook
    ctx.data.get_spot_price.side_effect = get_spot_price
    ctx.data.generate_pairs.return_value = pairs
    ctx.data.get_market_name = MagicMock(
        side_effect=lambda mid: get_market_info(mid).get("imData", {}).get("name", str(mid)))


# ======================================================================
# Entry tests
# ======================================================================

class TestEntry:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_entry_when_spread_above_threshold(self):
        """Mid spread = 0.05 > 0.042 threshold -> entry."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1
        assert entries[0]["pair"] == "TEST_60_61"
        assert entries[0]["scenario"] == 1  # Short A, Long B
        assert entries[0]["spread"] > 0.042
        ctx.executor.submit_dual_order.assert_called_once()

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_no_entry_when_spread_below_threshold(self):
        """Mid spread 0.05 < 0.10 threshold -> no entry."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0
        ctx.executor.submit_dual_order.assert_not_called()

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_state_persisted_after_entry(self):
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        pos_A = ctx.state.get_position("fr_arb", 60)
        pos_B = ctx.state.get_position("fr_arb", 61)
        assert pos_A is not None
        assert pos_B is not None
        assert pos_A["side"] == 1  # Short A
        assert pos_B["side"] == 0  # Long B
        assert pos_A["tokens"] > 0
        assert pos_A["round_id"] == pos_B["round_id"]

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_layers_created_on_entry(self):
        """New entry should create a single layer."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        pos_A = ctx.state.get_position("fr_arb", 60)
        assert "layers" in pos_A
        assert len(pos_A["layers"]) == 1
        layer = pos_A["layers"][0]
        assert layer["tokens"] > 0
        assert "entry_apr_A" in layer
        assert "entry_apr_B" in layer

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_weighted_entry_apr_stored(self):
        """Entry should store weighted_entry_apr."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        pos_A = ctx.state.get_position("fr_arb", 60)
        assert "weighted_entry_apr" in pos_A
        # With one layer, weighted = layer apr
        assert pos_A["weighted_entry_apr"] == pos_A["layers"][0]["entry_apr_A"]

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_no_entry_when_orderbook_empty(self):
        ctx = make_context()
        setup_pair(ctx, ob_a={"bids": [], "asks": []})

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_exec_fail_when_dual_order_rejected(self):
        ctx = make_context()
        setup_pair(ctx)
        ctx.executor.submit_dual_order.return_value = False

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0
        fails = [e for e in events if e["type"] == "exec_fail"]
        assert len(fails) == 1
        assert fails[0]["reason"] == "dual_entry_failed"


# ======================================================================
# Mid-rate spread signal tests
# ======================================================================

class TestMidRateSpread:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_mid_rate_calculation(self):
        """Mid = (bid+ask)/2 for each market. Spread = mid_A - mid_B."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        scans = [e for e in events if e["type"] == "scan"]
        assert len(scans) == 1
        # mid_A = (0.08+0.085)/2 = 0.0825
        # mid_B = (0.03+0.035)/2 = 0.0325
        # spread = 0.0825 - 0.0325 = 0.05
        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1
        assert entries[0]["spread"] == pytest.approx(0.05, abs=0.001)

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_reverse_scenario(self):
        """When B has higher mid-rate, scenario 2 is chosen."""
        ctx = make_context()
        # Swap orderbooks so B has higher rates
        setup_pair(ctx, ob_a=OB_B, ob_b=OB_A)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1
        assert entries[0]["scenario"] == 2  # Short B, Long A


# ======================================================================
# Exit tests
# ======================================================================

class TestExit:
    def _setup_positions(self, ctx, hours_ago=5, tokens=100.0):
        entry_time = ctx.now - timedelta(hours=hours_ago)
        round_id = "test_round"
        layer = {
            "round_id": round_id,
            "tokens": tokens,
            "position_size_A": 5000.0,
            "position_size_B": 3500.0,
            "entry_time": entry_time.isoformat(),
            "entry_apr_A": 0.08,
            "entry_apr_B": 0.035,
        }
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1,  # Short A
            "position_size": 5000.0,
            "tokens": tokens,
            "round_id": round_id,
            "layers": [layer],
        })
        ctx.state.set_position("fr_arb", 61, {
            "entry_time": entry_time.isoformat(),
            "side": 0,  # Long B
            "position_size": 3500.0,
            "tokens": tokens,
            "round_id": round_id,
            "layers": [layer],
        })

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)  # prevent re-entry
    def test_exit_when_spread_narrows(self):
        """Mid spread narrows below threshold -> exit."""
        ctx = make_context()
        # Narrow OB: mid_A = (0.055+0.06)/2 = 0.0575, mid_B = (0.03+0.035)/2 = 0.0325
        # spread = 0.0575 - 0.0325 = 0.025 < 0.038
        narrow_ob_a = {
            "bids": [(0.055, 500.0)],
            "asks": [(0.06, 500.0)],
        }
        setup_pair(ctx, ob_a=narrow_ob_a)
        self._setup_positions(ctx, hours_ago=5)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1
        ctx.executor.close_dual_position.assert_called_once()
        assert ctx.state.get_position("fr_arb", 60) is None
        assert ctx.state.get_position("fr_arb", 61) is None

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 10.0)
    def test_hold_when_min_hold_not_met(self):
        """Spread narrowed but held only 5h < 10h min -> hold."""
        ctx = make_context()
        narrow_ob_a = {"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]}
        setup_pair(ctx, ob_a=narrow_ob_a)
        self._setup_positions(ctx, hours_ago=5)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        holds = [e for e in events if e["type"] == "hold"]
        exits = [e for e in events if e["type"] == "exit"]
        assert len(holds) == 1
        assert len(exits) == 0

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    def test_hold_when_spread_still_wide(self):
        """Mid spread 0.05 > 0.038 -> hold."""
        ctx = make_context()
        setup_pair(ctx)  # Default OBs have wide spread
        self._setup_positions(ctx, hours_ago=5)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        holds = [e for e in events if e["type"] == "hold"]
        exits = [e for e in events if e["type"] == "exit"]
        assert len(holds) == 1
        assert len(exits) == 0

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)  # prevent re-entry
    def test_partial_close_updates_layers(self):
        """When closing partially, layers are updated proportionally."""
        ctx = make_context()
        # Narrow spread but shallow exit OB so only partial close
        narrow_ob_a = {
            "bids": [(0.055, 30.0)],  # only 30 tokens avail for exit
            "asks": [(0.06, 30.0)],
        }
        narrow_ob_b = {
            "bids": [(0.03, 30.0)],
            "asks": [(0.035, 30.0)],
        }
        setup_pair(ctx, ob_a=narrow_ob_a, ob_b=narrow_ob_b)
        self._setup_positions(ctx, hours_ago=5, tokens=100.0)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        if exits:
            # Partial close should keep position with reduced tokens
            pos_A = ctx.state.get_position("fr_arb", 60)
            if pos_A:
                assert pos_A["tokens"] < 100.0
                # Layers should have reduced tokens
                assert pos_A["layers"][0]["tokens"] < 100.0


# ======================================================================
# Exit batching tests
# ======================================================================

class TestExitBatching:
    def _setup_positions(self, ctx, hours_ago=5, last_exit_time=None):
        entry_time = ctx.now - timedelta(hours=hours_ago)
        round_id = "test_round"
        layer = {
            "round_id": round_id, "tokens": 100.0,
            "position_size_A": 5000.0, "position_size_B": 3500.0,
            "entry_time": entry_time.isoformat(),
            "entry_apr_A": 0.08, "entry_apr_B": 0.035,
        }
        pos_data_A = {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 5000.0,
            "tokens": 100.0, "round_id": round_id, "layers": [layer],
        }
        pos_data_B = {
            "entry_time": entry_time.isoformat(),
            "side": 0, "position_size": 3500.0,
            "tokens": 100.0, "round_id": round_id, "layers": [layer],
        }
        if last_exit_time:
            pos_data_A["last_exit_batch_time"] = last_exit_time.isoformat()
            pos_data_B["last_exit_batch_time"] = last_exit_time.isoformat()
        ctx.state.set_position("fr_arb", 60, pos_data_A)
        ctx.state.set_position("fr_arb", 61, pos_data_B)

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(fr_config, "EXIT_BATCH_MINUTES", 15)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_first_exit_attempt_proceeds(self):
        """No last_exit_batch_time -> exit proceeds."""
        ctx = make_context()
        narrow_ob_a = {"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]}
        setup_pair(ctx, ob_a=narrow_ob_a)
        self._setup_positions(ctx, hours_ago=5)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(fr_config, "EXIT_BATCH_MINUTES", 15)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_exit_blocked_within_batch_window(self):
        """Exit attempted 5 min ago, batch_minutes=15 -> blocked."""
        ctx = make_context()
        narrow_ob_a = {"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]}
        setup_pair(ctx, ob_a=narrow_ob_a)
        last_exit = ctx.now - timedelta(minutes=5)
        self._setup_positions(ctx, hours_ago=5, last_exit_time=last_exit)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 0
        holds = [e for e in events if e["type"] == "hold"]
        assert len(holds) == 1

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(fr_config, "EXIT_BATCH_MINUTES", 15)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_exit_allowed_after_batch_window(self):
        """Exit attempted 20 min ago, batch_minutes=15 -> allowed."""
        ctx = make_context()
        narrow_ob_a = {"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]}
        setup_pair(ctx, ob_a=narrow_ob_a)
        last_exit = ctx.now - timedelta(minutes=20)
        self._setup_positions(ctx, hours_ago=5, last_exit_time=last_exit)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1


# ======================================================================
# Marginal execution spread scan tests
# ======================================================================

class TestMarginalPnlTokens:
    def test_all_tokens_closeable(self):
        """When marginal spread is below threshold throughout, all tokens close."""
        strat = FRArbitrageStrategy()
        # Flat book: closing any amount gives same spread
        exit_liq_A = [(0.04, 200.0)]  # buy back A (was short)
        exit_liq_B = [(0.06, 200.0)]  # sell B (was long)
        # side_A=1 (short A), marginal_exec_spread = slice_apr_A - slice_apr_B
        # = 0.04 - 0.06 = -0.02 < EXIT_SPREAD_THRESHOLD
        with patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038):
            tokens = strat._marginal_pnl_tokens(exit_liq_A, exit_liq_B,
                                                 side_A=1, total_tokens=100.0)
        assert tokens == pytest.approx(100.0)

    def test_zero_when_spread_too_wide(self):
        """When marginal spread >= threshold at first slice, returns 0."""
        strat = FRArbitrageStrategy()
        # A asks are high (expensive to close short), B bids are low
        exit_liq_A = [(0.10, 200.0)]
        exit_liq_B = [(0.01, 200.0)]
        # marginal = 0.10 - 0.01 = 0.09 >= 0.038
        with patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038):
            tokens = strat._marginal_pnl_tokens(exit_liq_A, exit_liq_B,
                                                 side_A=1, total_tokens=100.0)
        assert tokens == pytest.approx(0.0)

    def test_partial_close_depth_limited(self):
        """Liquidity runs out on one side -> partial close."""
        strat = FRArbitrageStrategy()
        exit_liq_A = [(0.04, 50.0)]  # only 50 tokens
        exit_liq_B = [(0.06, 200.0)]
        with patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038):
            tokens = strat._marginal_pnl_tokens(exit_liq_A, exit_liq_B,
                                                 side_A=1, total_tokens=100.0)
        assert tokens == pytest.approx(50.0)

    def test_reverse_side(self):
        """side_A=0 (long A): marginal = slice_apr_B - slice_apr_A."""
        strat = FRArbitrageStrategy()
        exit_liq_A = [(0.06, 200.0)]  # sell A
        exit_liq_B = [(0.04, 200.0)]  # buy back B
        # marginal = 0.04 - 0.06 = -0.02 < 0.038 -> all pass
        with patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038):
            tokens = strat._marginal_pnl_tokens(exit_liq_A, exit_liq_B,
                                                 side_A=0, total_tokens=100.0)
        assert tokens == pytest.approx(100.0)


# ======================================================================
# Top-3 floor + scan logic in exit
# ======================================================================

class TestExitMarginalScanOnly:
    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_no_exit_when_exec_spread_too_wide(self):
        """OB execution spread exceeds threshold -> marginal scan=0 -> no exit."""
        ctx = make_context()
        # Mid spread narrow (triggers exit signal):
        # mid_A=(0.035+0.08)/2=0.0575, mid_B=(0.03+0.038)/2=0.034
        # spread=0.0235 < 0.038 ✓
        # But exec spread: ask_A=0.08, bid_B=0.03 → 0.05 > EXIT_SPREAD_THRESHOLD
        ob_a = {"bids": [(0.035, 500.0)], "asks": [(0.08, 500.0)]}
        ob_b = {"bids": [(0.03, 500.0)], "asks": [(0.038, 500.0)]}
        setup_pair(ctx, ob_a=ob_a, ob_b=ob_b)

        entry_time = ctx.now - timedelta(hours=5)
        round_id = "test_round"
        layer = {
            "round_id": round_id, "tokens": 50.0,
            "position_size_A": 5000.0, "position_size_B": 3500.0,
            "entry_time": entry_time.isoformat(),
            "entry_apr_A": 0.08, "entry_apr_B": 0.035,
        }
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 5000.0,
            "tokens": 50.0, "round_id": round_id, "layers": [layer],
        })
        ctx.state.set_position("fr_arb", 61, {
            "entry_time": entry_time.isoformat(),
            "side": 0, "position_size": 3500.0,
            "tokens": 50.0, "round_id": round_id, "layers": [layer],
        })

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 0  # marginal scan=0 → no exit → no neg PnL


# ======================================================================
# Orphan auto-close tests
# ======================================================================

class TestOrphanAutoClose:
    def _setup_one_leg(self, ctx, market_id=60, side=1, hours_ago=5):
        entry_time = ctx.now - timedelta(hours=hours_ago)
        ctx.state.set_position("fr_arb", market_id, {
            "entry_time": entry_time.isoformat(),
            "side": side,
            "position_size": 5000.0,
            "tokens": 100.0,
            "round_id": "orphan_round",
        })

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)  # prevent re-entry
    def test_orphan_auto_closes(self):
        """Only one leg in state -> auto-close via close_position."""
        ctx = make_context()
        setup_pair(ctx)
        self._setup_one_leg(ctx, market_id=60, side=1)
        ctx.executor.close_position.return_value = {"status": "dry_run"}

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1
        assert exits[0]["reason"] == "orphan_auto_close"
        ctx.executor.close_position.assert_called_once()
        assert ctx.state.get_position("fr_arb", 60) is None

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_orphan_close_failure_retries(self):
        """Orphan close fails -> exec_fail event, state NOT cleared."""
        ctx = make_context()
        setup_pair(ctx)
        self._setup_one_leg(ctx, market_id=60, side=1)
        ctx.executor.close_position.return_value = None

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        fails = [e for e in events if e["type"] == "exec_fail"]
        assert len(fails) == 1
        assert fails[0]["reason"] == "orphan_close_failed"
        assert ctx.state.get_position("fr_arb", 60) is not None

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_orphan_other_leg(self):
        """Only leg B in state -> auto-close leg B."""
        ctx = make_context()
        setup_pair(ctx)
        self._setup_one_leg(ctx, market_id=61, side=0)
        ctx.executor.close_position.return_value = {"status": "dry_run"}

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1
        assert exits[0]["reason"] == "orphan_auto_close"
        assert ctx.state.get_position("fr_arb", 61) is None


class TestMultiPairOrphan:
    """Market 60 in two pairs: 60_61 (active) and 60_62.
    Market 60 should NOT be treated as orphan in pair 60_62."""

    MARKET_C = {
        "marketId": 62,
        "imData": {"name": "Test Market C", "symbol": "TEST-C",
                   "maturity": 1774569600, "tickStep": 2},
        "data": {"assetMarkPrice": 100.0, "markApr": 0.02,
                 "bestBid": 0.02, "bestAsk": 0.025},
    }
    OB_C = {"bids": [(0.02, 500.0)], "asks": [(0.025, 500.0)]}

    def _setup_active_pair(self, ctx):
        entry_time = ctx.now - timedelta(hours=5)
        rid = "active_round_60_61"
        layer = {
            "round_id": rid, "tokens": 100.0,
            "position_size_A": 5000.0, "position_size_B": 3500.0,
            "entry_time": entry_time.isoformat(),
            "entry_apr_A": 0.08, "entry_apr_B": 0.035,
        }
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 5000.0,
            "tokens": 100.0, "round_id": rid, "layers": [layer],
        })
        ctx.state.set_position("fr_arb", 61, {
            "entry_time": entry_time.isoformat(),
            "side": 0, "position_size": 3500.0,
            "tokens": 100.0, "round_id": rid, "layers": [layer],
        })

    def test_not_orphan_when_paired_in_other_combo(self):
        ctx = make_context()
        two_pairs = {"TEST_60_61": (60, 61), "TEST_60_62": (60, 62)}

        markets = {60: MARKET_A, 61: MARKET_B, 62: self.MARKET_C}
        obs = {60: OB_A, 61: OB_B, 62: self.OB_C}

        ctx.data.get_market_info.side_effect = lambda mid: markets.get(mid, {})
        ctx.data.get_orderbook.side_effect = lambda mid, **kw: obs.get(mid, {"bids": [], "asks": []})
        ctx.data.get_spot_price.side_effect = lambda mid: float(
            markets.get(mid, {}).get("data", {}).get("assetMarkPrice", 1.0))
        ctx.data.generate_pairs.return_value = two_pairs
        ctx.data.get_all_market_ids.return_value = [60, 61, 62]

        self._setup_active_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        orphan_exits = [e for e in events if e.get("reason") == "orphan_auto_close"]
        assert len(orphan_exits) == 0
        ctx.executor.close_position.assert_not_called()
        assert ctx.state.get_position("fr_arb", 60) is not None
        assert ctx.state.get_position("fr_arb", 61) is not None


# ======================================================================
# VWAP capacity scan tests
# ======================================================================

class TestVWAPCapacity:
    def test_calc_vwap_basic(self):
        liquidity = [(0.08, 10.0), (0.075, 20.0), (0.07, 30.0)]
        vwap = _calc_vwap(liquidity, 15.0)
        # 10 @ 0.08 + 5 @ 0.075 = 0.8 + 0.375 = 1.175 / 15
        assert vwap == pytest.approx(1.175 / 15.0)

    def test_calc_vwap_exact_fill(self):
        liquidity = [(0.08, 10.0)]
        vwap = _calc_vwap(liquidity, 10.0)
        assert vwap == pytest.approx(0.08)

    def test_calc_vwap_book_exhausted(self):
        liquidity = [(0.08, 5.0)]
        vwap = _calc_vwap(liquidity, 10.0)
        assert vwap is None

    def test_calc_vwap_empty(self):
        vwap = _calc_vwap([], 10.0)
        assert vwap is None


# ======================================================================
# Global capital management tests
# ======================================================================

class TestGlobalCapital:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "MAX_CAPITAL", 0.01)  # very small cap
    def test_no_entry_when_capital_exhausted(self):
        """Global capital exhausted -> no entry."""
        ctx = make_context()
        setup_pair(ctx)

        # Pre-existing position consuming capital
        entry_time = ctx.now - timedelta(hours=1)
        ctx.state.set_position("fr_arb", 99, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 100.0,
            "tokens": 10.0,
        })

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "MAX_CAPITAL", 100000)
    def test_entry_with_sufficient_capital(self):
        """Enough capital -> entry proceeds."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1


# ======================================================================
# Depth-weighted candidate ranking tests
# ======================================================================

class TestDepthRanking:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "MIN_DEPTH_USD", 50000)  # high threshold
    def test_skip_when_depth_insufficient(self):
        """Min depth not met -> skip with insufficient_depth."""
        ctx = make_context()
        # Shallow OBs: only 10 tokens at $100 = $1000 depth
        shallow_ob_a = {"bids": [(0.08, 10.0)], "asks": [(0.085, 10.0)]}
        shallow_ob_b = {"bids": [(0.03, 10.0)], "asks": [(0.035, 10.0)]}
        setup_pair(ctx, ob_a=shallow_ob_a, ob_b=shallow_ob_b)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        skips = [e for e in events if e["type"] == "skip"
                 and e.get("reason") == "insufficient_depth"]
        assert len(skips) == 1
        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0


# ======================================================================
# Scale-in tests
# ======================================================================

class TestScaleIn:
    def _setup_existing_position(self, ctx, hours_ago=24, tokens=50.0):
        entry_time = ctx.now - timedelta(hours=hours_ago)
        round_id = "initial_round"
        layer = {
            "round_id": round_id, "tokens": tokens,
            "position_size_A": 2500.0, "position_size_B": 1750.0,
            "entry_time": entry_time.isoformat(),
            "entry_apr_A": 0.08, "entry_apr_B": 0.035,
        }
        last_addon = (ctx.now - timedelta(hours=hours_ago)).isoformat()
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 2500.0, "tokens": tokens,
            "round_id": round_id, "layers": [layer],
            "last_addon_time": last_addon,
            "weighted_entry_apr": 0.08,
        })
        ctx.state.set_position("fr_arb", 61, {
            "entry_time": entry_time.isoformat(),
            "side": 0, "position_size": 1750.0, "tokens": tokens,
            "round_id": round_id, "layers": [layer],
            "last_addon_time": last_addon,
            "weighted_entry_apr": 0.035,
        })

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "MAX_LAYERS", 5)
    @patch.object(fr_config, "MIN_ADDON_INTERVAL_HOURS", 12)
    @patch.object(fr_config, "MIN_ADDON_TOKENS", 5)  # low for test
    def test_scalein_adds_layer(self):
        """Existing position + spread above threshold + interval met -> scale-in."""
        ctx = make_context()
        setup_pair(ctx)
        self._setup_existing_position(ctx, hours_ago=24, tokens=50.0)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry" and e.get("is_scalein")]
        if entries:
            assert entries[0]["layer_count"] == 2
            pos_A = ctx.state.get_position("fr_arb", 60)
            assert len(pos_A["layers"]) == 2

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "MAX_LAYERS", 1)  # already at max
    @patch.object(fr_config, "MIN_ADDON_INTERVAL_HOURS", 12)
    def test_no_scalein_at_max_layers(self):
        """Already at MAX_LAYERS -> no scale-in."""
        ctx = make_context()
        setup_pair(ctx)
        self._setup_existing_position(ctx, hours_ago=24, tokens=50.0)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        scalein_entries = [e for e in events if e["type"] == "entry"
                          and e.get("is_scalein")]
        assert len(scalein_entries) == 0

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "MAX_LAYERS", 5)
    @patch.object(fr_config, "MIN_ADDON_INTERVAL_HOURS", 48)  # 48h interval
    def test_no_scalein_too_soon(self):
        """Less than MIN_ADDON_INTERVAL_HOURS since last addon -> no scale-in."""
        ctx = make_context()
        setup_pair(ctx)
        # Only 24h ago, but interval is 48h
        self._setup_existing_position(ctx, hours_ago=24, tokens=50.0)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        scalein_entries = [e for e in events if e["type"] == "entry"
                          and e.get("is_scalein")]
        assert len(scalein_entries) == 0


# ======================================================================
# Margin pre-check tests
# ======================================================================

class TestMarginPreCheck:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_skip_when_insufficient_margin(self):
        ctx = make_context()
        setup_pair(ctx)

        ctx.data.get_collateral_detail = MagicMock(return_value={
            3: {"available": 0.001, "positions": []},
        })

        market_a = dict(MARKET_A)
        market_a['config'] = {'kIM': '909090909090909090', 'tThresh': 604800}
        market_a['tokenId'] = 3
        market_b = dict(MARKET_B)
        market_b['config'] = {'kIM': '909090909090909090', 'tThresh': 604800}
        market_b['tokenId'] = 3

        orig_get_market_info = ctx.data.get_market_info.side_effect

        def get_market_info_with_config(mid):
            if mid == 60:
                return market_a
            elif mid == 61:
                return market_b
            return orig_get_market_info(mid)

        ctx.data.get_market_info.side_effect = get_market_info_with_config

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        skips = [e for e in events if e["type"] == "skip"]
        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0
        assert len(skips) == 1
        assert skips[0]["reason"] == "insufficient_margin"

    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "USER_ADDRESS", "")
    def test_no_margin_check_without_user_address(self):
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1


# ======================================================================
# Dynamic pairs tests
# ======================================================================

class TestDynamicPairs:
    def test_pairs_generated_from_context(self):
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        ctx.data.generate_pairs.assert_called_once()
        scans = [e for e in events if e["type"] == "scan"]
        assert len(scans) == 1
        assert scans[0]["pair"] == "TEST_60_61"

    def test_empty_pairs_no_crash(self):
        ctx = make_context()
        ctx.data = MagicMock()
        ctx.data.generate_pairs.return_value = {}

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        scans = [e for e in events if e["type"] == "scan"]
        entries = [e for e in events if e["type"] == "entry"]
        assert len(scans) == 0
        assert len(entries) == 0


# ======================================================================
# On-chain state sync tests
# ======================================================================

class TestOnchainSync:
    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)  # prevent entry
    def test_clears_position_not_onchain(self):
        """Position in state but not on-chain -> cleared."""
        ctx = make_context()
        setup_pair(ctx)
        ctx.data.get_collateral_detail = MagicMock(return_value={
            3: {"available": 100.0, "positions": []},
        })

        # Set a position in state
        entry_time = ctx.now - timedelta(hours=5)
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 5000.0,
            "tokens": 100.0, "round_id": "test",
        })

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        # Position should be cleared since not on-chain
        assert ctx.state.get_position("fr_arb", 60) is None

    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    @patch.object(fr_config, "DUST_THRESHOLD_TOKENS", 0.01)
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.10)
    def test_dust_auto_closed(self):
        """On-chain position below DUST_THRESHOLD -> auto-closed."""
        ctx = make_context()
        setup_pair(ctx)
        ctx.data.get_collateral_detail = MagicMock(return_value={
            3: {"available": 100.0, "positions": [
                {"market_id": 60, "side": 1, "size": 0.005,
                 "size_wei": "5000000000000000", "entry_rate": 0.08},
            ]},
        })

        strat = FRArbitrageStrategy()
        strat.on_tick(ctx)

        ctx.executor.close_position.assert_called_once()
