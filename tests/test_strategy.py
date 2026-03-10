"""Tests for FRArbitrageStrategy - mock context, test entry/exit/hold logic."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from strategies.fr_arb.strategy import FRArbitrageStrategy
from strategies.fr_arb import config as fr_config
from strategies.framework.interfaces import IContext, IExecutor
from strategies.framework.state_manager import InMemoryStateManager


# Two markets forming a pair: A has higher rates, B has lower rates
# Using spot_price=100 so token_value (0.08*100=8) allows sufficient capacity
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
    ctx.data.get_market_name = MagicMock(side_effect=lambda mid: get_market_info(mid).get("imData", {}).get("name", str(mid)))


class TestEntry:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    def test_entry_when_spread_above_threshold(self):
        """Bid_A(0.08) - Ask_B(0.035) = 0.045 > 0.042 threshold -> entry."""
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
        """Spread 0.045 < 0.10 threshold -> no entry."""
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
        # Atomic dual order fails
        ctx.executor.submit_dual_order.return_value = False

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0
        fails = [e for e in events if e["type"] == "exec_fail"]
        assert len(fails) == 1
        assert fails[0]["reason"] == "dual_entry_failed"


class TestExit:
    def _setup_positions(self, ctx, hours_ago=5):
        entry_time = ctx.now - timedelta(hours=hours_ago)
        round_id = "test_round"
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1,  # Short A
            "position_size": 5000.0,
            "tokens": 100.0,
            "round_id": round_id,
        })
        ctx.state.set_position("fr_arb", 61, {
            "entry_time": entry_time.isoformat(),
            "side": 0,  # Long B
            "position_size": 3500.0,
            "tokens": 100.0,
            "round_id": round_id,
        })

    @patch.object(fr_config, "EXIT_SPREAD_THRESHOLD", 0.038)
    @patch.object(fr_config, "MIN_HOLD_HOURS", 1.0)
    def test_exit_when_spread_narrows(self):
        """Close spread = ask_A(0.085) - bid_B(0.03) = 0.055 > 0.038, no exit.
        Override OB so spread narrows below threshold."""
        ctx = make_context()
        # Narrowed spread: ask_A=0.06, bid_B=0.03 -> spread=0.03 < 0.038
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
        """Spread still wide (0.045 > 0.038) -> hold."""
        ctx = make_context()
        setup_pair(ctx)  # Default OBs have wide spread
        self._setup_positions(ctx, hours_ago=5)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        holds = [e for e in events if e["type"] == "hold"]
        exits = [e for e in events if e["type"] == "exit"]
        assert len(holds) == 1
        assert len(exits) == 0


class TestOrphanAutoClose:
    def _setup_one_leg(self, ctx, market_id=60, side=1, hours_ago=5):
        """Set up only one leg of a pair in state."""
        entry_time = ctx.now - timedelta(hours=hours_ago)
        ctx.state.set_position("fr_arb", market_id, {
            "entry_time": entry_time.isoformat(),
            "side": side,
            "position_size": 5000.0,
            "tokens": 100.0,
            "round_id": "orphan_round",
        })

    def test_orphan_auto_closes(self):
        """Only one leg in state → auto-close via close_position."""
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
        # State should be cleared
        assert ctx.state.get_position("fr_arb", 60) is None

    def test_orphan_close_failure_retries(self):
        """Orphan close fails → exec_fail event, state NOT cleared (retry next tick)."""
        ctx = make_context()
        setup_pair(ctx)
        self._setup_one_leg(ctx, market_id=60, side=1)
        ctx.executor.close_position.return_value = None  # close fails

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        fails = [e for e in events if e["type"] == "exec_fail"]
        assert len(fails) == 1
        assert fails[0]["reason"] == "orphan_close_failed"
        # State should NOT be cleared — will retry next tick
        assert ctx.state.get_position("fr_arb", 60) is not None

    def test_orphan_other_leg(self):
        """Only leg B in state (leg A missing) → auto-close leg B."""
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
    """Market 60 appears in two pairs: 60_61 (active) and 60_62.
    Market 60 should NOT be treated as orphan in pair 60_62."""

    MARKET_C = {
        "marketId": 62,
        "imData": {"name": "Test Market C", "symbol": "TEST-C",
                   "maturity": 1774569600, "tickStep": 2},
        "data": {"assetMarkPrice": 100.0, "markApr": 0.02,
                 "bestBid": 0.02, "bestAsk": 0.025},
    }

    OB_C = {
        "bids": [(0.02, 500.0)],
        "asks": [(0.025, 500.0)],
    }

    def _setup_active_pair(self, ctx):
        """Market 60 (SHORT) + 61 (LONG) with shared round_id."""
        entry_time = ctx.now - timedelta(hours=5)
        rid = "active_round_60_61"
        ctx.state.set_position("fr_arb", 60, {
            "entry_time": entry_time.isoformat(),
            "side": 1, "position_size": 5000.0,
            "tokens": 100.0, "round_id": rid,
        })
        ctx.state.set_position("fr_arb", 61, {
            "entry_time": entry_time.isoformat(),
            "side": 0, "position_size": 3500.0,
            "tokens": 100.0, "round_id": rid,
        })

    def test_not_orphan_when_paired_in_other_combo(self):
        """Market 60 is in pair 60_61 (active). In pair 60_62 it should
        NOT be treated as orphan — it has a partner (61) via dynamic pair check."""
        ctx = make_context()

        # Two pairs share market 60
        two_pairs = {"TEST_60_61": (60, 61), "TEST_60_62": (60, 62)}

        markets = {60: MARKET_A, 61: MARKET_B, 62: self.MARKET_C}
        obs = {60: OB_A, 61: OB_B, 62: self.OB_C}

        ctx.data.get_market_info.side_effect = lambda mid: markets.get(mid, {})
        ctx.data.get_orderbook.side_effect = lambda mid, **kw: obs.get(mid, {"bids": [], "asks": []})
        ctx.data.get_spot_price.side_effect = lambda mid: float(markets.get(mid, {}).get("data", {}).get("assetMarkPrice", 1.0))
        ctx.data.generate_pairs.return_value = two_pairs
        ctx.data.get_all_market_ids.return_value = [60, 61, 62]

        self._setup_active_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        # No orphan close should happen — market 60 has partner 61
        orphan_exits = [e for e in events if e.get("reason") == "orphan_auto_close"]
        assert len(orphan_exits) == 0
        ctx.executor.close_position.assert_not_called()

        # Both positions still in state
        assert ctx.state.get_position("fr_arb", 60) is not None
        assert ctx.state.get_position("fr_arb", 61) is not None


class TestVWAPCapacity:
    def test_calculate_vwap_basic(self):
        liquidity = [(0.08, 10.0), (0.075, 20.0), (0.07, 30.0)]
        vwap = FRArbitrageStrategy._calculate_vwap(liquidity, 15.0)
        # 10 @ 0.08 + 5 @ 0.075 = 0.8 + 0.375 = 1.175 / 15 = 0.0783
        assert vwap == pytest.approx(1.175 / 15.0)

    def test_calculate_vwap_exact_fill(self):
        liquidity = [(0.08, 10.0)]
        vwap = FRArbitrageStrategy._calculate_vwap(liquidity, 10.0)
        assert vwap == pytest.approx(0.08)

    def test_calculate_vwap_book_exhausted(self):
        liquidity = [(0.08, 5.0)]
        vwap = FRArbitrageStrategy._calculate_vwap(liquidity, 10.0)
        assert vwap is None

    def test_calculate_vwap_empty(self):
        vwap = FRArbitrageStrategy._calculate_vwap([], 10.0)
        assert vwap is None


class TestMarginPreCheck:
    @patch.object(fr_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(fr_config, "USER_ADDRESS", "0xTestUser")
    def test_skip_when_insufficient_margin(self):
        """Spread qualifies but margin is too low -> skip with insufficient_margin."""
        ctx = make_context()
        setup_pair(ctx)

        # Mock collateral detail returning near-zero balance, no positions
        ctx.data.get_collateral_detail = MagicMock(return_value={
            3: {"available": 0.001, "positions": []},
        })

        # Add config fields to market info for IM calculation
        market_a = dict(MARKET_A)
        market_a['config'] = {
            'kIM': '909090909090909090',  # 0.9091
            'tThresh': 604800,
        }
        market_a['tokenId'] = 3
        market_b = dict(MARKET_B)
        market_b['config'] = {
            'kIM': '909090909090909090',
            'tThresh': 604800,
        }
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
        """Without USER_ADDRESS, margin check is skipped and entry proceeds."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1


class TestDynamicPairs:
    def test_pairs_generated_from_context(self):
        """Strategy uses generate_pairs from data provider, not config."""
        ctx = make_context()
        setup_pair(ctx)

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        ctx.data.generate_pairs.assert_called_once()
        scans = [e for e in events if e["type"] == "scan"]
        assert len(scans) == 1
        assert scans[0]["pair"] == "TEST_60_61"

    def test_empty_pairs_no_crash(self):
        """No pairs generated -> no scan/entry/exit events, no crash."""
        ctx = make_context()
        ctx.data = MagicMock()
        ctx.data.generate_pairs.return_value = {}

        strat = FRArbitrageStrategy()
        events = strat.on_tick(ctx)

        scans = [e for e in events if e["type"] == "scan"]
        entries = [e for e in events if e["type"] == "entry"]
        assert len(scans) == 0
        assert len(entries) == 0
