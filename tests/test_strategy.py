"""Tests for VShapeStrategy - mock context, test entry/exit/hold logic."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from strategies.v_shape.strategy import VShapeStrategy
from strategies.v_shape import config
from strategies.framework.interfaces import IContext, IDataProvider, IExecutor
from strategies.framework.state_manager import InMemoryStateManager
from strategies.framework.data_provider import BorosDataProvider
from tests.conftest import MARKET_23, MARKET_24, ORDERBOOK_23_RAW, ORDERBOOK_24_RAW

# Deep orderbook for entry tests (enough USD depth at these rates)
DEEP_OB = {
    "long": {
        "ia": [40, 35, 30, 25, 20],
        "sz": [str(int(50000 * 1e18))] * 5,
    },
    "short": {
        "ia": [50, 55, 60, 65, 70],
        "sz": [str(int(50000 * 1e18))] * 5,
    },
}

# Market with higher rates so USD depth is sufficient
MARKET_DEEP = {
    "marketId": 24,
    "imData": {
        "name": "Test ETH Market",
        "symbol": "TEST-ETH",
        "maturity": 1774569600,
        "tickStep": 2,
    },
    "config": {},
    "metadata": {"platformName": "Test"},
    "data": {
        "markApr": 0.04,
        "bestBid": 0.04,
        "bestAsk": 0.05,
        "assetMarkPrice": 2000.0,
        "floatingApr": -0.07,
    },
}


def make_context(now=None):
    ctx = MagicMock(spec=IContext)
    ctx.now = now or datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ctx.state = InMemoryStateManager()
    ctx.executor = MagicMock(spec=IExecutor)
    ctx.executor.submit_order.return_value = True
    ctx.executor.close_position.return_value = {"status": "dry_run"}
    # Don't use spec= here because strategy uses BorosDataProvider helpers
    # (get_market_name, etc.) that extend beyond the IDataProvider interface
    ctx.data = MagicMock()
    return ctx


def setup_single_market(ctx, market_id, market_info, ob_raw, funding_rate):
    """Wire mock data provider for one market."""
    ctx.data.get_all_market_ids.return_value = [market_id]
    ctx.data.get_market_name.side_effect = lambda mid: market_info.get('imData', {}).get('name', str(mid))
    ctx.data.get_market_info.side_effect = lambda mid: market_info if mid == market_id else {}
    ctx.data.get_oracle_funding_rate.side_effect = lambda mid: funding_rate if mid == market_id else 0.0
    ctx.data.get_spot_price.side_effect = lambda mid: float(
        market_info.get('data', {}).get('assetMarkPrice', 1.0)
    ) if mid == market_id else 1.0

    bids = BorosDataProvider._parse_ob_side(ob_raw.get('long', {}), 0.001)
    asks = BorosDataProvider._parse_ob_side(ob_raw.get('short', {}), 0.001)
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    ctx.data.get_orderbook.side_effect = lambda mid, **kw: {'bids': bids, 'asks': asks}


class TestEntrySignal:
    def test_entry_when_funding_below_threshold(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_DEEP, DEEP_OB, funding_rate=-0.08)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e['type'] == 'entry']
        assert len(entries) == 1
        assert entries[0]['market_id'] == 24
        assert entries[0]['funding_rate'] == -0.08
        ctx.executor.submit_order.assert_called_once()

    def test_no_entry_when_funding_above_threshold(self):
        ctx = make_context()
        setup_single_market(ctx, 23, MARKET_23, ORDERBOOK_23_RAW, funding_rate=0.01)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e['type'] == 'entry']
        assert len(entries) == 0
        ctx.executor.submit_order.assert_not_called()

    def test_no_entry_at_exact_threshold(self):
        ctx = make_context()
        setup_single_market(ctx, 23, MARKET_23, ORDERBOOK_23_RAW,
                            funding_rate=config.ENTRY_THRESHOLD)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)
        entries = [e for e in events if e['type'] == 'entry']
        assert len(entries) == 0

    def test_state_persisted_after_entry(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_DEEP, DEEP_OB, funding_rate=-0.08)

        strat = VShapeStrategy()
        strat.on_tick(ctx)

        pos = ctx.state.get_position("v_shape", 24)
        assert pos is not None
        assert pos['entry_rate'] == -0.08
        assert pos['position_size'] > 0
        assert pos['tokens'] > 0

    def test_max_positions_cap(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_24, ORDERBOOK_24_RAW, funding_rate=-0.08)

        # Pre-fill state with MAX_POSITIONS positions
        for i in range(config.MAX_POSITIONS):
            ctx.state.set_position("v_shape", 100 + i, {
                "entry_time": ctx.now.isoformat(), "entry_rate": -0.1,
                "position_size": 1000, "tokens": 10,
            })

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        entries = [e for e in events if e['type'] == 'entry']
        skips = [e for e in events if e['type'] == 'skip' and e.get('reason') == 'max_positions']
        assert len(entries) == 0
        assert len(skips) == 1


class TestExitSignal:
    def _setup_position(self, ctx, market_id, hours_ago, entry_rate=-0.08):
        entry_time = ctx.now - timedelta(hours=hours_ago)
        ctx.state.set_position("v_shape", market_id, {
            "entry_time": entry_time.isoformat(),
            "entry_rate": entry_rate,
            "position_size": 1000,
            "tokens": 10.0,
            "round_id": "test_round",
        })

    def test_exit_when_funding_recovered_and_held_enough(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_24, ORDERBOOK_24_RAW, funding_rate=0.01)
        self._setup_position(ctx, 24, hours_ago=15)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        exits = [e for e in events if e['type'] == 'exit']
        assert len(exits) == 1
        assert exits[0]['market_id'] == 24
        ctx.executor.close_position.assert_called_once()
        assert ctx.state.get_position("v_shape", 24) is None

    def test_hold_when_funding_recovered_but_too_early(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_24, ORDERBOOK_24_RAW, funding_rate=0.01)
        self._setup_position(ctx, 24, hours_ago=2)  # < MIN_HOLD_HOURS

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        holds = [e for e in events if e['type'] == 'hold']
        exits = [e for e in events if e['type'] == 'exit']
        assert len(holds) == 1
        assert len(exits) == 0

    def test_hold_when_funding_still_negative(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_24, ORDERBOOK_24_RAW, funding_rate=-0.03)
        self._setup_position(ctx, 24, hours_ago=20)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        holds = [e for e in events if e['type'] == 'hold']
        exits = [e for e in events if e['type'] == 'exit']
        assert len(holds) == 1
        assert len(exits) == 0


class TestDataUnavailable:
    def test_skip_when_funding_rate_none(self):
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_DEEP, DEEP_OB, funding_rate=-0.08)
        # Override funding rate to return None (API failure)
        ctx.data.get_oracle_funding_rate.side_effect = lambda mid: None

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        skips = [e for e in events if e['type'] == 'skip']
        assert len(skips) == 1
        assert skips[0]['reason'] == 'data_unavailable'
        assert skips[0]['funding_rate'] is None
        ctx.executor.submit_order.assert_not_called()

    def test_no_exit_when_funding_rate_none_with_position(self):
        """If funding data unavailable, hold existing position (don't exit)."""
        ctx = make_context()
        setup_single_market(ctx, 24, MARKET_DEEP, DEEP_OB, funding_rate=-0.08)
        ctx.data.get_oracle_funding_rate.side_effect = lambda mid: None

        from datetime import timedelta
        entry_time = ctx.now - timedelta(hours=15)
        ctx.state.set_position("v_shape", 24, {
            "entry_time": entry_time.isoformat(),
            "entry_rate": -0.08,
            "position_size": 1000,
            "tokens": 10.0,
        })

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        # Should skip, not exit
        exits = [e for e in events if e['type'] == 'exit']
        skips = [e for e in events if e['type'] == 'skip']
        assert len(exits) == 0
        assert len(skips) == 1
        assert skips[0]['reason'] == 'data_unavailable'
        # Position should still exist
        assert ctx.state.get_position("v_shape", 24) is not None


class TestEventTypes:
    def test_scan_events_for_all_markets(self):
        ctx = make_context()
        setup_single_market(ctx, 23, MARKET_23, ORDERBOOK_23_RAW, funding_rate=0.01)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        scans = [e for e in events if e['type'] == 'scan']
        assert len(scans) == 1
        assert scans[0]['market_id'] == 23
        assert 'funding_rate' in scans[0]
        assert 'spot_price' in scans[0]
        assert 'best_bid' in scans[0]

    def test_skip_event_has_reason(self):
        ctx = make_context()
        # Tiny orderbook -> insufficient depth
        tiny_ob = {
            "long": {"ia": [1], "sz": ["1000000000000"]},  # tiny
            "short": {"ia": [2], "sz": ["1000000000000"]},
        }
        setup_single_market(ctx, 24, MARKET_24, tiny_ob, funding_rate=-0.08)

        strat = VShapeStrategy()
        events = strat.on_tick(ctx)

        skips = [e for e in events if e['type'] == 'skip']
        assert len(skips) == 1
        assert skips[0]['reason'] == 'insufficient_depth'
