"""Tests for capacity / depth calculation."""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from strategies.v_shape.capacity import calculate_available_depth, get_depth_constrained_size
from strategies.framework.interfaces import IContext, IDataProvider
from tests.conftest import MARKET_23


# Realistic orderbook: (rate, tokens)
BOOK = {
    "bids": [(0.021, 0.5), (0.020, 1.0), (0.019, 2.0), (0.018, 3.0)],
    "asks": [(0.023, 0.8), (0.024, 1.5), (0.025, 2.5), (0.026, 4.0)],
}


class TestCalculateAvailableDepth:
    def test_short_uses_bids(self):
        tokens, usd = calculate_available_depth(
            BOOK, side=1, spot_price=68000.0, time_remaining_years=0.05, n_levels=10
        )
        # All 4 bid levels: 0.5 + 1.0 + 2.0 + 3.0 = 6.5 tokens
        assert tokens == pytest.approx(6.5)
        # USD = sum(tokens_i * |rate_i| * 0.05 * 68000)
        expected_usd = (
            0.5 * 0.021 * 0.05 * 68000
            + 1.0 * 0.020 * 0.05 * 68000
            + 2.0 * 0.019 * 0.05 * 68000
            + 3.0 * 0.018 * 0.05 * 68000
        )
        assert usd == pytest.approx(expected_usd)

    def test_long_uses_asks(self):
        tokens, usd = calculate_available_depth(
            BOOK, side=0, spot_price=68000.0, time_remaining_years=0.05, n_levels=10
        )
        assert tokens == pytest.approx(8.8)

    def test_n_levels_limit(self):
        tokens, _ = calculate_available_depth(
            BOOK, side=1, spot_price=68000.0, time_remaining_years=0.05, n_levels=2
        )
        assert tokens == pytest.approx(1.5)  # only first 2 levels

    def test_empty_book(self):
        tokens, usd = calculate_available_depth(
            {"bids": [], "asks": []}, side=1, spot_price=68000.0,
            time_remaining_years=0.05
        )
        assert tokens == 0.0
        assert usd == 0.0

    def test_negative_rates(self):
        book = {"bids": [(-0.04, 10.0), (-0.05, 20.0)], "asks": []}
        tokens, usd = calculate_available_depth(
            book, side=1, spot_price=100.0, time_remaining_years=1.0
        )
        assert tokens == 30.0
        # USD uses abs(rate)
        expected = 10.0 * 0.04 * 1.0 * 100.0 + 20.0 * 0.05 * 1.0 * 100.0
        assert usd == pytest.approx(expected)


class TestGetDepthConstrainedSize:
    def _make_context(self, book, spot_price, maturity):
        ctx = MagicMock(spec=IContext)
        ctx.now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        ctx.data = MagicMock(spec=IDataProvider)
        ctx.data.get_orderbook.return_value = book
        ctx.data.get_spot_price.return_value = spot_price
        ctx.data.get_market_info.return_value = MARKET_23
        return ctx

    def test_sufficient_depth(self):
        ctx = self._make_context(BOOK, 68000.0, 1774569600)
        size = get_depth_constrained_size(ctx, 23, side=1, base_size=100,
                                          min_depth_multiplier=1.0, depth_usage_pct=0.5)
        assert size > 0
        assert size <= 100

    def test_insufficient_depth_returns_zero(self):
        tiny_book = {"bids": [(0.001, 0.001)], "asks": []}
        ctx = self._make_context(tiny_book, 68000.0, 1774569600)
        size = get_depth_constrained_size(ctx, 23, side=1, base_size=100000,
                                          min_depth_multiplier=1.0, depth_usage_pct=0.3)
        assert size == 0.0

    def test_caps_at_base_size(self):
        # Very deep book should cap at base_size
        deep_book = {"bids": [(0.05, 100000.0)], "asks": []}
        ctx = self._make_context(deep_book, 68000.0, 1774569600)
        size = get_depth_constrained_size(ctx, 23, side=1, base_size=1000,
                                          min_depth_multiplier=1.0, depth_usage_pct=0.5)
        assert size == 1000.0
