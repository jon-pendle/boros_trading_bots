"""Tests for PricingEngine - pure math, no mocks needed."""
from strategies.framework.pricing import PricingEngine


class TestRateToTick:
    def test_positive_rate(self):
        assert PricingEngine.rate_to_tick(0.025, 0.001) == 25

    def test_negative_rate(self):
        assert PricingEngine.rate_to_tick(-0.03, 0.001) == -30

    def test_fractional_floor(self):
        # 0.0215 / 0.001 = 21.5 -> floor = 21
        assert PricingEngine.rate_to_tick(0.0215, 0.001) == 21

    def test_zero_tick_step(self):
        assert PricingEngine.rate_to_tick(0.05, 0) == 0

    def test_zero_rate(self):
        assert PricingEngine.rate_to_tick(0.0, 0.001) == 0

    def test_large_tick_step(self):
        # tickStep=2 from market config, rate=0.021
        assert PricingEngine.rate_to_tick(0.021, 2) == 0
        assert PricingEngine.rate_to_tick(5.0, 2) == 2


class TestCalculateLimitTick:
    def test_short_side_positive_rates(self):
        # SHORT: target = 0.02 - |0.02| * 0.05 = 0.02 - 0.001 = 0.019
        tick = PricingEngine.calculate_limit_tick(
            side=1, best_bid=0.02, best_ask=0.025, tick_step=0.001, slippage=0.05
        )
        assert tick == 19  # floor(0.019 / 0.001)

    def test_long_side_positive_rates(self):
        # LONG: target = 0.025 + |0.025| * 0.05 = 0.025 + 0.00125 = 0.02625
        tick = PricingEngine.calculate_limit_tick(
            side=0, best_bid=0.02, best_ask=0.025, tick_step=0.001, slippage=0.05
        )
        assert tick == 26  # floor(0.02625 / 0.001)

    def test_zero_slippage(self):
        tick = PricingEngine.calculate_limit_tick(
            side=1, best_bid=0.03, best_ask=0.035, tick_step=0.001, slippage=0.0
        )
        assert tick == 30  # floor(0.03 / 0.001)

    def test_short_negative_rates(self):
        # SHORT: target = -0.04 - |0.04| * 0.05 = -0.04 - 0.002 = -0.042
        # Accepts more negative (worse) rate — correct slippage direction
        tick = PricingEngine.calculate_limit_tick(
            side=1, best_bid=-0.04, best_ask=-0.035, tick_step=0.001, slippage=0.05
        )
        assert tick == -42  # floor(-0.042 / 0.001)

    def test_long_negative_rates(self):
        # LONG: target = -0.035 + |0.035| * 0.05 = -0.035 + 0.00175 = -0.03325
        # Accepts less negative (worse) rate — correct slippage direction
        tick = PricingEngine.calculate_limit_tick(
            side=0, best_bid=-0.04, best_ask=-0.035, tick_step=0.001, slippage=0.05
        )
        assert tick == -34  # floor(-0.03325 / 0.001)
