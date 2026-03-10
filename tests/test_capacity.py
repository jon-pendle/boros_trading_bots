"""Tests for FR Arb VWAP capacity, margin, and pair generation."""
import pytest
import time
from strategies.fr_arb.strategy import FRArbitrageStrategy
from strategies.framework.pricing import PricingEngine
from strategies.framework.data_provider import BorosDataProvider


class TestCalculateVWAP:
    def test_single_level_exact_fill(self):
        liquidity = [(0.05, 100.0)]
        assert FRArbitrageStrategy._calculate_vwap(liquidity, 100.0) == pytest.approx(0.05)

    def test_multiple_levels(self):
        liquidity = [(0.05, 10.0), (0.04, 20.0), (0.03, 30.0)]
        # Fill 25 tokens: 10@0.05 + 15@0.04 = 0.5 + 0.6 = 1.1 / 25
        vwap = FRArbitrageStrategy._calculate_vwap(liquidity, 25.0)
        assert vwap == pytest.approx(1.1 / 25.0)

    def test_book_exhausted_returns_none(self):
        liquidity = [(0.05, 10.0)]
        assert FRArbitrageStrategy._calculate_vwap(liquidity, 20.0) is None

    def test_empty_book_returns_none(self):
        assert FRArbitrageStrategy._calculate_vwap([], 10.0) is None

    def test_zero_tokens(self):
        liquidity = [(0.05, 10.0)]
        assert FRArbitrageStrategy._calculate_vwap(liquidity, 0.0) == 0


class TestCalculateIMPerToken:
    def test_basic_im_formula(self):
        """Verify the raw formula (low margin_floor so it doesn't dominate)."""
        im = PricingEngine.calculate_im_per_token(
            rate=0.08, k_im=0.9091,
            t_thresh_seconds=604800,
            i_tick_thresh=393, tick_step=2,
            margin_floor=0.001,  # low floor to test formula
            time_to_maturity_seconds=18 * 86400,  # 18 days
        )
        # rate_floor = 1.00005^(393*2) - 1 ≈ 0.04009
        # rate_factor = max(0.08, 0.04009) = 0.08
        # ttm_years = 18*86400/31536000 ≈ 0.0493
        # IM = 0.08 * 0.9091 * 0.0493 ≈ 0.003585
        assert im == pytest.approx(0.003585, rel=0.01)

    def test_margin_floor_dominates(self):
        """When calculated IM < margin_floor, floor is returned."""
        im = PricingEngine.calculate_im_per_token(
            rate=0.08, k_im=0.9091,
            t_thresh_seconds=604800,
            i_tick_thresh=393, tick_step=2,
            margin_floor=0.04,  # higher than calculated 0.003585
            time_to_maturity_seconds=18 * 86400,
        )
        assert im == pytest.approx(0.04)

    def test_rate_floor_used_when_rate_low(self):
        """When rate < rate_floor, the floor is used."""
        im = PricingEngine.calculate_im_per_token(
            rate=0.01, k_im=0.9091,
            t_thresh_seconds=604800,
            i_tick_thresh=393, tick_step=2,
            margin_floor=0.001,  # low floor
            time_to_maturity_seconds=18 * 86400,
        )
        # rate_floor ≈ 0.04009 > 0.01 -> uses rate_floor
        # IM = 0.04009 * 0.9091 * 0.0493 ≈ 0.001797
        assert im == pytest.approx(0.001797, rel=0.01)

    def test_margin_floor_minimum(self):
        """IM should never be below margin_floor."""
        im = PricingEngine.calculate_im_per_token(
            rate=0.001, k_im=0.01,
            t_thresh_seconds=100,
            i_tick_thresh=1, tick_step=1,
            margin_floor=0.05,
            time_to_maturity_seconds=100,
        )
        assert im >= 0.05

    def test_t_thresh_used_when_ttm_small(self):
        """When TTM < tThresh, tThresh is used."""
        im_short_ttm = PricingEngine.calculate_im_per_token(
            rate=0.08, k_im=0.9091,
            t_thresh_seconds=864000,  # 10 days
            i_tick_thresh=393, tick_step=2,
            margin_floor=0.04,
            time_to_maturity_seconds=3 * 86400,  # 3 days < 10 days
        )
        im_with_tthresh = PricingEngine.calculate_im_per_token(
            rate=0.08, k_im=0.9091,
            t_thresh_seconds=864000,
            i_tick_thresh=393, tick_step=2,
            margin_floor=0.04,
            time_to_maturity_seconds=864000,  # exactly tThresh
        )
        assert im_short_ttm == pytest.approx(im_with_tthresh)


class TestParseBaseAsset:
    def test_btcusdt(self):
        assert BorosDataProvider._parse_base_asset("BINANCE-BTCUSDT-27MAR2026") == "BTC"

    def test_eth_no_suffix(self):
        assert BorosDataProvider._parse_base_asset("HYPERLIQUID-ETH-27MAR2026") == "ETH"

    def test_xyz_gold(self):
        assert BorosDataProvider._parse_base_asset("HYPERLIQUID-xyzGOLD-27MAR2026") == "GOLD"

    def test_xau_maps_to_gold(self):
        assert BorosDataProvider._parse_base_asset("BINANCE-XAUUSDT-27MAR2026") == "GOLD"

    def test_invalid_symbol(self):
        assert BorosDataProvider._parse_base_asset("INVALID") is None

    def test_empty(self):
        assert BorosDataProvider._parse_base_asset("") is None


class TestGeneratePairs:
    def _make_provider_with_markets(self, markets):
        dp = BorosDataProvider.__new__(BorosDataProvider)
        dp._market_cache = {int(m['marketId']): m for m in markets}
        dp.base_url = ""
        dp.timeout = 10
        return dp

    def test_generates_pairs_same_asset_maturity_collateral(self):
        future_mat = int(time.time()) + 86400 * 30
        markets = [
            {"marketId": 60, "state": "Normal", "tokenId": 3,
             "imData": {"symbol": "HYPERLIQUID-BTC-27MAR2026", "maturity": future_mat}},
            {"marketId": 61, "state": "Normal", "tokenId": 3,
             "imData": {"symbol": "HYPERLIQUID-BTC-27MAR2026", "maturity": future_mat}},
            {"marketId": 62, "state": "Normal", "tokenId": 3,
             "imData": {"symbol": "GATE-BTCUSDT-27MAR2026", "maturity": future_mat}},
        ]
        dp = self._make_provider_with_markets(markets)
        pairs = dp.generate_pairs()
        assert len(pairs) == 3  # C(3,2) = 3

    def test_different_collateral_not_paired(self):
        """BTC markets with different tokenId should NOT be paired."""
        future_mat = int(time.time()) + 86400 * 30
        markets = [
            {"marketId": 23, "state": "Normal", "tokenId": 1,  # WBTC collateral
             "imData": {"symbol": "BINANCE-BTCUSDT-27MAR2026", "maturity": future_mat}},
            {"marketId": 60, "state": "Normal", "tokenId": 3,  # USDT collateral
             "imData": {"symbol": "HYPERLIQUID-BTC-27MAR2026", "maturity": future_mat}},
        ]
        dp = self._make_provider_with_markets(markets)
        pairs = dp.generate_pairs()
        assert len(pairs) == 0

    def test_no_pairs_from_single_market(self):
        future_mat = int(time.time()) + 86400 * 30
        markets = [
            {"marketId": 58, "state": "Normal", "tokenId": 3,
             "imData": {"symbol": "HYPERLIQUID-SOL-27MAR2026", "maturity": future_mat}},
        ]
        dp = self._make_provider_with_markets(markets)
        pairs = dp.generate_pairs()
        assert len(pairs) == 0

    def test_expired_markets_excluded(self):
        past_mat = int(time.time()) - 86400
        markets = [
            {"marketId": 48, "state": "Normal", "tokenId": 1,
             "imData": {"symbol": "BINANCE-BTCUSDT-27FEB2026", "maturity": past_mat}},
            {"marketId": 49, "state": "Normal", "tokenId": 1,
             "imData": {"symbol": "OKX-BTCUSDT-27FEB2026", "maturity": past_mat}},
        ]
        dp = self._make_provider_with_markets(markets)
        pairs = dp.generate_pairs()
        assert len(pairs) == 0

    def test_different_assets_not_paired(self):
        future_mat = int(time.time()) + 86400 * 30
        markets = [
            {"marketId": 23, "state": "Normal", "tokenId": 3,
             "imData": {"symbol": "BINANCE-BTCUSDT-27MAR2026", "maturity": future_mat}},
            {"marketId": 24, "state": "Normal", "tokenId": 3,
             "imData": {"symbol": "BINANCE-ETHUSDT-27MAR2026", "maturity": future_mat}},
        ]
        dp = self._make_provider_with_markets(markets)
        pairs = dp.generate_pairs()
        assert len(pairs) == 0
