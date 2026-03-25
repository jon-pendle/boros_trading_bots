"""Tests for ZScoreStrategy — pair-level state, z-score signal, shared markets."""
import json
import pytest
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from strategies.zscore.strategy import ZScoreStrategy, _calc_vwap, HISTORY_FILE, POSITIONS_FILE
from strategies.zscore import config as zs_config
from strategies.framework.interfaces import IContext, IExecutor
from strategies.framework.state_manager import InMemoryStateManager


@pytest.fixture(autouse=True)
def _isolate_zscore_files(tmp_path):
    """Prevent tests from reading/writing real position/history files."""
    with patch("strategies.zscore.strategy.POSITIONS_FILE", str(tmp_path / "pos.json")), \
         patch("strategies.zscore.strategy.HISTORY_FILE", str(tmp_path / "hist.json")):
        yield


MARKET_A = {
    "marketId": 60,
    "imData": {"name": "Test A", "symbol": "TEST-A", "maturity": 1774569600, "tickStep": 2},
    "data": {"assetMarkPrice": 100.0, "markApr": 0.08, "bestBid": 0.08, "bestAsk": 0.085},
}
MARKET_B = {
    "marketId": 61,
    "imData": {"name": "Test B", "symbol": "TEST-B", "maturity": 1774569600, "tickStep": 2},
    "data": {"assetMarkPrice": 100.0, "markApr": 0.03, "bestBid": 0.03, "bestAsk": 0.035},
}
MARKET_C = {
    "marketId": 62,
    "imData": {"name": "Test C", "symbol": "TEST-C", "maturity": 1774569600, "tickStep": 2},
    "data": {"assetMarkPrice": 100.0, "markApr": 0.02, "bestBid": 0.02, "bestAsk": 0.025},
}
OB_A = {"bids": [(0.08, 500.0), (0.075, 500.0)], "asks": [(0.085, 500.0), (0.09, 500.0)]}
OB_B = {"bids": [(0.03, 500.0), (0.025, 500.0)], "asks": [(0.035, 500.0), (0.04, 500.0)]}
OB_C = {"bids": [(0.02, 500.0)], "asks": [(0.025, 500.0)]}
TEST_PAIR = {"TEST_60_61": (60, 61)}


def make_context(now=None):
    ctx = MagicMock(spec=IContext)
    ctx.now = now or datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ctx.state = InMemoryStateManager()
    ctx.executor = MagicMock(spec=IExecutor)
    ctx.executor.submit_dual_order.return_value = True
    ctx.executor.close_position.return_value = {"status": "dry_run"}
    ctx.executor.close_dual_position.return_value = {"status": "dry_run"}
    ctx.data = MagicMock()
    return ctx


def setup_pair(ctx, ob_a=None, ob_b=None, pairs=None):
    ob_a = ob_a or OB_A
    ob_b = ob_b or OB_B
    pairs = pairs or TEST_PAIR
    markets = {60: MARKET_A, 61: MARKET_B, 62: MARKET_C}
    obs = {60: ob_a, 61: ob_b, 62: OB_C}
    ctx.data.get_market_info.side_effect = lambda mid: markets.get(mid, {})
    ctx.data.get_orderbook.side_effect = lambda mid, **kw: obs.get(mid, {"bids": [], "asks": []})
    ctx.data.get_spot_price.side_effect = lambda mid: 100.0
    ctx.data.generate_pairs.return_value = pairs


def _warmup(strat, pair_name="TEST_60_61", n=250, base_spread=0.05):
    np.random.seed(42)
    for _ in range(n):
        strat._get_zscore(pair_name, base_spread + np.random.normal(0, 0.002))


def _make_pair_pos(ctx, pair_name="TEST_60_61", hours_ago=5, tokens=100.0,
                   mkt_A=60, mkt_B=61, side_A=1, side_B=0):
    """Set up a pair-level position in state."""
    entry_time = ctx.now - timedelta(hours=hours_ago)
    layer = {
        "round_id": "test_round", "tokens": tokens,
        "position_size_A": 5000.0, "position_size_B": 3500.0,
        "entry_time": entry_time.isoformat(),
        "entry_apr_A": 0.08, "entry_apr_B": 0.035,
    }
    ctx.state.set_position("zscore", pair_name, {
        "mkt_A": mkt_A, "mkt_B": mkt_B,
        "side_A": side_A, "side_B": side_B,
        "entry_time": entry_time.isoformat(),
        "tokens": tokens,
        "position_size_A": 5000.0, "position_size_B": 3500.0,
        "entry_rate_A": 0.08, "entry_rate_B": 0.035,
        "round_id": "test_round",
        "layers": [layer],
    })


class TestZScoreComputation:
    def test_zscore_needs_min_observations(self):
        strat = ZScoreStrategy()
        with patch.object(zs_config, "LOOKBACK", 100):
            for i in range(49):
                z = strat._get_zscore("pair1", 0.05 + i * 0.0001)
            assert z is None

    def test_zscore_returns_value_after_warmup(self):
        strat = ZScoreStrategy()
        with patch.object(zs_config, "LOOKBACK", 40), \
             patch.object(zs_config, "USE_MAD", True):
            for i in range(25):
                z = strat._get_zscore("pair1", 0.05 + np.random.normal(0, 0.001))
            assert z is not None

    def test_zscore_mad_vs_std(self):
        strat_mad = ZScoreStrategy()
        strat_std = ZScoreStrategy()
        np.random.seed(42)
        data = list(np.random.normal(0.05, 0.002, 30))
        data.append(0.20)
        with patch.object(zs_config, "LOOKBACK", 40), \
             patch.object(zs_config, "USE_MAD", True):
            for v in data:
                z_mad = strat_mad._get_zscore("p1", v)
        with patch.object(zs_config, "LOOKBACK", 40), \
             patch.object(zs_config, "USE_MAD", False):
            for v in data:
                z_std = strat_std._get_zscore("p2", v)
        assert abs(z_mad) > abs(z_std)


class TestPairLevelState:
    def test_pair_state_stored_with_string_key(self):
        """State keyed by pair_name string, not market_id int."""
        ctx = make_context()
        setup_pair(ctx)

        strat = ZScoreStrategy()
        _warmup(strat, base_spread=0.01, n=250)

        with patch.object(zs_config, "K_ENTRY", 1.0):
            events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        if entries:
            pos = ctx.state.get_position("zscore", "TEST_60_61")
            assert pos is not None
            assert pos["mkt_A"] == 60
            assert pos["mkt_B"] == 61
            assert pos["side_A"] in (0, 1)
            assert pos["side_B"] in (0, 1)
            # No market-level records
            assert ctx.state.get_position("zscore", 60) is None
            assert ctx.state.get_position("zscore", 61) is None

    def test_namespace_isolation(self):
        """ZScore pair state doesn't interfere with fr_arb market state."""
        ctx = make_context()
        ctx.state.set_position("fr_arb", 60, {"side": 1, "tokens": 100})
        _make_pair_pos(ctx, "TEST_60_61")

        assert ctx.state.get_position("fr_arb", 60) is not None
        assert ctx.state.get_position("zscore", "TEST_60_61") is not None
        assert ctx.state.get_position("zscore", 60) is None


class TestSharedMarkets:
    """Market 60 appears in TEST_60_61 and TEST_60_62. Each pair tracks independently."""

    TWO_PAIRS = {"TEST_60_61": (60, 61), "TEST_60_62": (60, 62)}

    @patch.object(zs_config, "K_ENTRY", 1.0)
    @patch.object(zs_config, "LOOKBACK", 480)
    @patch.object(zs_config, "USE_MAD", True)
    def test_independent_entries(self):
        """Both pairs can open positions sharing market 60."""
        ctx = make_context()
        setup_pair(ctx, pairs=self.TWO_PAIRS)

        strat = ZScoreStrategy()
        _warmup(strat, "TEST_60_61", base_spread=0.01, n=250)
        _warmup(strat, "TEST_60_62", base_spread=0.01, n=250)

        events = strat.on_tick(ctx)
        entries = [e for e in events if e["type"] == "entry"]

        # Both may enter (spread is extreme for both)
        for e in entries:
            pair = e["pair"]
            pos = ctx.state.get_position("zscore", pair)
            assert pos is not None
            assert pos["mkt_A"] == 60  # shared market

        # If both entered, verify separate state records
        if len(entries) == 2:
            pos_61 = ctx.state.get_position("zscore", "TEST_60_61")
            pos_62 = ctx.state.get_position("zscore", "TEST_60_62")
            assert pos_61 is not None
            assert pos_62 is not None
            assert pos_61["mkt_B"] == 61
            assert pos_62["mkt_B"] == 62

    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_shared_market_exits_independently(self):
        """Both pairs sharing market 60 can exit independently.
        Each pair closes only its own tokens, not the other's."""
        ctx = make_context()
        setup_pair(ctx, pairs=self.TWO_PAIRS,
                   ob_a={"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]})

        _make_pair_pos(ctx, "TEST_60_61", mkt_A=60, mkt_B=61, tokens=50.0)
        _make_pair_pos(ctx, "TEST_60_62", mkt_A=60, mkt_B=62, tokens=30.0)

        strat = ZScoreStrategy()
        np.random.seed(42)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.06 + np.random.normal(0, 0.005))
            strat._get_zscore("TEST_60_62", 0.06 + np.random.normal(0, 0.005))

        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        # Both can exit — each closes its own tokens
        for ex in exits:
            if ex["pair"] == "TEST_60_61":
                assert ex["tokens_closed"] <= 50.0
            elif ex["pair"] == "TEST_60_62":
                assert ex["tokens_closed"] <= 30.0
        # Verify both had separate close_dual_position calls
        assert ctx.executor.close_dual_position.call_count == len(exits)


class TestZScoreEntry:
    @patch.object(zs_config, "K_ENTRY", 1.0)
    @patch.object(zs_config, "LOOKBACK", 480)
    @patch.object(zs_config, "USE_MAD", True)
    def test_entry_on_extreme_zscore(self):
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        _warmup(strat, base_spread=0.01, n=250)
        events = strat.on_tick(ctx)
        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 1
        ctx.executor.submit_dual_order.assert_called_once()

    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "LOOKBACK", 480)
    def test_no_entry_when_zscore_below_threshold(self):
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        _warmup(strat, base_spread=0.05, n=250)
        events = strat.on_tick(ctx)
        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0

    @patch.object(zs_config, "K_ENTRY", 1.0)
    @patch.object(zs_config, "LOOKBACK", 480)
    def test_no_entry_during_warmup(self):
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        for _ in range(10):
            strat._get_zscore("TEST_60_61", 0.05)
        events = strat.on_tick(ctx)
        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0


class TestZScoreExit:
    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_exit_on_mean_reversion(self):
        ctx = make_context()
        narrow_ob_a = {"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]}
        setup_pair(ctx, ob_a=narrow_ob_a)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=5)
        np.random.seed(42)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.06 + np.random.normal(0, 0.005))
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1
        ctx.executor.close_dual_position.assert_called_once()
        assert ctx.state.get_position("zscore", "TEST_60_61") is None

    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_hold_when_zscore_still_extreme(self):
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=5)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.01)
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        holds = [e for e in events if e["type"] == "hold"]
        assert len(exits) == 0
        assert len(holds) == 1


class TestZNoneExit:
    @patch.object(zs_config, "Z_NONE_EXIT_HOURS", 48)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_force_exit_when_z_none_timeout(self):
        """Position held >48h with z=None -> force exit."""
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=50)  # 50h > 48h threshold
        # Don't warm up z-score -> z will be None
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1
        assert "z=None" in exits[0]["reason"]

    @patch.object(zs_config, "Z_NONE_EXIT_HOURS", 48)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "LOOKBACK", 40)
    def test_hold_when_z_none_under_timeout(self):
        """Position held <48h with z=None -> hold (not forced yet)."""
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=24)  # 24h < 48h
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        holds = [e for e in events if e["type"] == "hold"]
        assert len(exits) == 0
        assert len(holds) == 1


class TestSpreadHistoryPersistence:
    @patch.object(zs_config, "LOOKBACK", 100)
    def test_save_and_load(self, tmp_path):
        hist_file = str(tmp_path / "test_history.json")
        with patch("strategies.zscore.strategy.HISTORY_FILE", hist_file):
            strat1 = ZScoreStrategy()
            np.random.seed(42)
            for _ in range(50):
                strat1._get_zscore("pair_A", 0.05 + np.random.normal(0, 0.002))
                strat1._get_zscore("pair_B", 0.03 + np.random.normal(0, 0.001))
            strat1._save_spread_history()

            assert Path(hist_file).exists()
            strat2 = ZScoreStrategy()
            assert len(strat2._spread_history["pair_A"]) == 50
            z = strat2._get_zscore("pair_A", 0.08)
            assert z is not None
            assert abs(z) > 1.0

    def test_missing_file_starts_fresh(self, tmp_path):
        hist_file = str(tmp_path / "nonexistent.json")
        with patch("strategies.zscore.strategy.HISTORY_FILE", hist_file):
            strat = ZScoreStrategy()
            assert len(strat._spread_history) == 0


class TestStateManagerStringKeys:
    def test_string_keys_preserved_in_get_all(self):
        """InMemoryStateManager preserves string keys in get_all_positions."""
        sm = InMemoryStateManager()
        sm.set_position("zscore", "ETH_74_75", {"mkt_A": 74, "tokens": 5})
        sm.set_position("zscore", "ETH_74_76", {"mkt_A": 74, "tokens": 3})
        sm.set_position("fr_arb", 74, {"side": 1, "tokens": 10})

        zs_all = sm.get_all_positions("zscore")
        assert "ETH_74_75" in zs_all
        assert "ETH_74_76" in zs_all
        assert len(zs_all) == 2

        fra_all = sm.get_all_positions("fr_arb")
        assert 74 in fra_all
        assert len(fra_all) == 1

    def test_get_set_clear_with_string_key(self):
        sm = InMemoryStateManager()
        sm.set_position("zscore", "PAIR_A_B", {"tokens": 10})
        assert sm.get_position("zscore", "PAIR_A_B")["tokens"] == 10
        sm.clear_position("zscore", "PAIR_A_B")
        assert sm.get_position("zscore", "PAIR_A_B") is None
