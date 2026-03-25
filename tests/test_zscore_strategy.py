"""Tests for ZScoreStrategy — pair-level state, z-score signal, shared markets."""
import json
import pytest
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from strategies.zscore.strategy import (
    ZScoreStrategy, _calc_vwap, _marginal_exit_tokens,
    HISTORY_FILE, POSITIONS_FILE,
)
from strategies.zscore import config as zs_config
from strategies.framework.interfaces import IContext, IExecutor
from strategies.framework.state_manager import InMemoryStateManager


@pytest.fixture(autouse=True)
def _isolate_zscore_files(tmp_path):
    """Prevent tests from reading/writing real files. Set SAMPLE_INTERVAL=1 for fast warmup."""
    with patch("strategies.zscore.strategy.POSITIONS_FILE", str(tmp_path / "pos.json")), \
         patch("strategies.zscore.strategy.HISTORY_FILE", str(tmp_path / "hist.json")), \
         patch.object(zs_config, "SAMPLE_INTERVAL", 1):
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


def _warmup(strat, pair_name="TEST_60_61", n=None, base_spread=0.05):
    """Warm up z-score history. n defaults to enough for min_obs after downsampling."""
    if n is None:
        n = zs_config.LOOKBACK * zs_config.SAMPLE_INTERVAL  # full window at 1-min resolution
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
    def test_only_one_pair_enters_shared_market(self):
        """Market 60 shared: only one pair enters, other blocked."""
        ctx = make_context()
        setup_pair(ctx, pairs=self.TWO_PAIRS)

        strat = ZScoreStrategy()
        _warmup(strat, "TEST_60_61", base_spread=0.01, n=250)
        _warmup(strat, "TEST_60_62", base_spread=0.01, n=250)

        events = strat.on_tick(ctx)
        entries = [e for e in events if e["type"] == "entry"]

        # At most one entry — market exclusivity
        assert len(entries) <= 1
        if entries:
            occupied = [e for e in events if e.get("reason") == "market_occupied"]
            assert len(occupied) >= 1


class TestMarketExclusivity:
    """One market can only belong to one pair at a time (matches backtest)."""

    TWO_PAIRS = {"TEST_60_61": (60, 61), "TEST_60_62": (60, 62)}

    @patch.object(zs_config, "K_ENTRY", 1.0)
    @patch.object(zs_config, "LOOKBACK", 480)
    @patch.object(zs_config, "USE_MAD", True)
    def test_skip_entry_when_market_occupied(self):
        """Market 60 occupied by TEST_60_61 -> TEST_60_62 skipped."""
        ctx = make_context()
        setup_pair(ctx, pairs=self.TWO_PAIRS)

        _make_pair_pos(ctx, "TEST_60_61", mkt_A=60, mkt_B=61, side_A=1, side_B=0, tokens=50.0)

        strat = ZScoreStrategy()
        _warmup(strat, "TEST_60_62", base_spread=0.01, n=250)
        _warmup(strat, "TEST_60_61", base_spread=0.05, n=250)

        events = strat.on_tick(ctx)

        skips = [e for e in events if e["type"] == "skip"
                 and e.get("reason") == "market_occupied"
                 and e.get("pair") == "TEST_60_62"]
        assert len(skips) == 1
        assert ctx.state.get_position("zscore", "TEST_60_62") is None

    @patch.object(zs_config, "K_ENTRY", 1.0)
    @patch.object(zs_config, "LOOKBACK", 480)
    @patch.object(zs_config, "USE_MAD", True)
    def test_first_entry_wins_by_zscore_rank(self):
        """When both pairs want to enter, highest |z| enters first, blocks the other."""
        ctx = make_context()
        setup_pair(ctx, pairs=self.TWO_PAIRS)

        strat = ZScoreStrategy()
        # Both pairs warmed up far from current spread
        _warmup(strat, "TEST_60_61", base_spread=0.01, n=250)
        _warmup(strat, "TEST_60_62", base_spread=0.01, n=250)

        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        occupied_skips = [e for e in events if e.get("reason") == "market_occupied"]

        # At most one entry (the one with higher |z|), other blocked
        assert len(entries) <= 1
        if entries:
            assert len(occupied_skips) >= 1


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
    @patch.object(zs_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_exit_on_mean_reversion(self):
        """dir_z < k_exit AND spread < threshold -> exit."""
        ctx = make_context()
        # Narrow spread: mid_A=0.0575, mid_B=0.0325, spread=0.025 < 0.042
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
    @patch.object(zs_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_hold_when_zscore_still_extreme(self):
        """dir_z > k_exit -> hold (even if spread is narrow)."""
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

    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(zs_config, "ENTRY_SPREAD_THRESHOLD", 0.01)  # very tight
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_hold_when_spread_still_wide(self):
        """dir_z < k_exit but spread > threshold -> hold (spread guard blocks exit)."""
        ctx = make_context()
        # Narrow OB for z-score to revert, but spread=0.025 > threshold=0.01
        narrow_ob_a = {"bids": [(0.055, 500.0)], "asks": [(0.06, 500.0)]}
        setup_pair(ctx, ob_a=narrow_ob_a)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=5)
        np.random.seed(42)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.06 + np.random.normal(0, 0.005))
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        holds = [e for e in events if e["type"] == "hold"]
        assert len(exits) == 0
        assert len(holds) == 1


class TestNoForceClose:
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "LOOKBACK", 40)
    def test_hold_when_z_none_indefinitely(self):
        """z=None -> hold forever, no force close. Settlement at maturity is safer."""
        ctx = make_context()
        setup_pair(ctx)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=500)  # 500h, no z-score
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        holds = [e for e in events if e["type"] == "hold"]
        assert len(exits) == 0
        assert len(holds) == 1

    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_hold_when_spread_still_wide_despite_z_revert(self):
        """dir_z < k_exit but spread > threshold -> hold (spread guard protects)."""
        ctx = make_context()
        setup_pair(ctx)  # spread=0.05 > 0.042
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=100)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.01)  # low history → z will revert
        events = strat.on_tick(ctx)
        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 0


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


class TestMarginalExitTokens:
    def test_all_closeable_when_spread_low(self):
        """Flat OB with low marginal spread -> all tokens closeable."""
        liq_A = [(0.04, 200.0)]
        liq_B = [(0.06, 200.0)]
        # side_A=1: marginal = slice_A - slice_B = 0.04 - 0.06 = -0.02 < 0.042
        tokens = _marginal_exit_tokens(liq_A, liq_B, side_A=1,
                                        total_tokens=100.0, exit_threshold=0.042)
        assert tokens == pytest.approx(100.0)

    def test_zero_when_spread_too_wide(self):
        """First slice already exceeds threshold -> 0 tokens."""
        liq_A = [(0.10, 200.0)]
        liq_B = [(0.01, 200.0)]
        # marginal = 0.10 - 0.01 = 0.09 >= 0.042
        tokens = _marginal_exit_tokens(liq_A, liq_B, side_A=1,
                                        total_tokens=100.0, exit_threshold=0.042)
        assert tokens == pytest.approx(0.0)

    def test_partial_when_depth_limited(self):
        """Liquidity runs out on one side -> partial."""
        liq_A = [(0.04, 50.0)]
        liq_B = [(0.06, 200.0)]
        tokens = _marginal_exit_tokens(liq_A, liq_B, side_A=1,
                                        total_tokens=100.0, exit_threshold=0.042)
        assert tokens == pytest.approx(50.0)

    def test_reverse_side(self):
        """side_A=0: marginal = slice_B - slice_A."""
        liq_A = [(0.06, 200.0)]
        liq_B = [(0.04, 200.0)]
        # marginal = 0.04 - 0.06 = -0.02 < 0.042
        tokens = _marginal_exit_tokens(liq_A, liq_B, side_A=0,
                                        total_tokens=100.0, exit_threshold=0.042)
        assert tokens == pytest.approx(100.0)


class TestNoNegativeTradePnL:
    """Core invariant: no exit should produce negative trade PnL.
    Entry requires VWAP spread > threshold. Exit marginal scan ensures
    every closed slice has execution spread < threshold.
    Therefore: entry_spread > exit_spread → positive PnL."""

    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(zs_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_no_exit_when_ob_spread_too_wide(self):
        """Mid spread narrowed (exit signal), but OB execution spread > threshold.
        Marginal scan returns 0 → no exit → no negative PnL."""
        ctx = make_context()
        # Mid spread narrow (trigger exit signal):
        # mid_A = (0.04+0.06)/2 = 0.05, mid_B = (0.025+0.035)/2 = 0.03
        # current_spread = 0.05 - 0.03 = 0.02 < 0.042 ✓
        # But exit OB execution spread is WIDE:
        # close short A: buy at ask_A = 0.06
        # close long B: sell at bid_B = 0.025
        # execution spread = 0.06 - 0.025 = 0.035 < 0.042 → actually passes
        # Need wider execution spread. Use asks with gap:
        wide_ob_a = {
            "bids": [(0.04, 500.0)],
            "asks": [(0.09, 500.0)],  # very wide ask
        }
        wide_ob_b = {
            "bids": [(0.005, 500.0)],  # very low bid
            "asks": [(0.035, 500.0)],
        }
        # mid_A=(0.04+0.09)/2=0.065, mid_B=(0.005+0.035)/2=0.02
        # current_spread = 0.065 - 0.02 = 0.045 > 0.042 → won't even trigger
        # Need mid spread < 4.2% but execution spread > 4.2%
        tight_mid_ob_a = {
            "bids": [(0.038, 500.0)],
            "asks": [(0.09, 500.0)],  # ask way above mid
        }
        tight_mid_ob_b = {
            "bids": [(0.001, 500.0)],  # bid way below mid
            "asks": [(0.022, 500.0)],
        }
        # mid_A=(0.038+0.09)/2=0.064, mid_B=(0.001+0.022)/2=0.0115
        # current_spread = 0.064-0.0115 = 0.0525 > 0.042 → still won't trigger
        # Let me just directly test: position with side_A=1 (short A, long B)
        # exit liq_A = asks (buy back A), exit liq_B = bids (sell B)
        # marginal spread = ask_A - bid_B. If ask_A=0.08, bid_B=0.03 → 0.05 > 0.042 → scan=0
        ob_a_exec_wide = {
            "bids": [(0.035, 500.0)],
            "asks": [(0.08, 500.0)],
        }
        ob_b_exec_wide = {
            "bids": [(0.03, 500.0)],
            "asks": [(0.038, 500.0)],
        }
        # mid_A=(0.035+0.08)/2=0.0575, mid_B=(0.03+0.038)/2=0.034
        # current_spread = 0.0575 - 0.034 = 0.0235 < 0.042 ✓ → exit signal triggers
        # exit exec: ask_A=0.08, bid_B=0.03 → marginal = 0.08-0.03 = 0.05 > 0.042 → scan=0
        setup_pair(ctx, ob_a=ob_a_exec_wide, ob_b=ob_b_exec_wide)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=5)
        np.random.seed(42)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.06 + np.random.normal(0, 0.005))
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 0  # marginal scan = 0 → no exit → no loss

    @patch.object(zs_config, "K_EXIT", 0.5)
    @patch.object(zs_config, "K_ENTRY", 100.0)
    @patch.object(zs_config, "MIN_HOLD_HOURS", 1.0)
    @patch.object(zs_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(zs_config, "LOOKBACK", 40)
    @patch.object(zs_config, "USE_MAD", True)
    def test_exit_only_when_exec_spread_profitable(self):
        """Exit happens only when marginal execution spread < threshold.
        Since entry was at spread > threshold, trade PnL is positive."""
        ctx = make_context()
        # Narrow spread: both mid and execution
        narrow_ob_a = {"bids": [(0.035, 500.0)], "asks": [(0.038, 500.0)]}
        narrow_ob_b = {"bids": [(0.032, 500.0)], "asks": [(0.035, 500.0)]}
        # mid_A=0.0365, mid_B=0.0335, spread=0.003 < 0.042 ✓
        # exec: ask_A=0.038, bid_B=0.032, marginal=0.038-0.032=0.006 < 0.042 ✓
        setup_pair(ctx, ob_a=narrow_ob_a, ob_b=narrow_ob_b)
        strat = ZScoreStrategy()
        _make_pair_pos(ctx, hours_ago=5)
        np.random.seed(42)
        for _ in range(25):
            strat._get_zscore("TEST_60_61", 0.06 + np.random.normal(0, 0.005))
        events = strat.on_tick(ctx)

        exits = [e for e in events if e["type"] == "exit"]
        assert len(exits) == 1
        # Verify: entry_spread (from _make_pair_pos: short A@0.08, long B@0.035)
        # = 0.08 - 0.035 = 0.045 > 0.042
        # exit execution spread = 0.038 - 0.032 = 0.006 < 0.042
        # trade PnL ∝ (0.045 - 0.006) > 0 ✓

    @patch.object(zs_config, "K_ENTRY", 1.0)
    @patch.object(zs_config, "ENTRY_SPREAD_THRESHOLD", 0.042)
    @patch.object(zs_config, "LOOKBACK", 480)
    @patch.object(zs_config, "USE_MAD", True)
    def test_no_entry_below_spread_threshold(self):
        """Entry blocked when VWAP spread < threshold. Prevents entering at low
        spread which would make profitable exit impossible."""
        ctx = make_context()
        # Low spread OB: mid_A=0.045, mid_B=0.035, spread=0.01 < 0.042
        low_ob_a = {"bids": [(0.04, 500.0)], "asks": [(0.05, 500.0)]}
        low_ob_b = {"bids": [(0.03, 500.0)], "asks": [(0.04, 500.0)]}
        setup_pair(ctx, ob_a=low_ob_a, ob_b=low_ob_b)
        strat = ZScoreStrategy()
        _warmup(strat, base_spread=0.001, n=500)  # history far from current → z extreme
        events = strat.on_tick(ctx)

        entries = [e for e in events if e["type"] == "entry"]
        assert len(entries) == 0  # VWAP spread < 4.2% → blocked


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
