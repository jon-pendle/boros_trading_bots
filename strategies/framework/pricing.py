import math


class PricingEngine:
    @staticmethod
    def rate_to_tick(rate: float, tick_step: float) -> int:
        """Converts a floating point rate into the integer Tick index used by the protocol."""
        if tick_step == 0:
            return 0
        return math.floor(rate / tick_step)

    @staticmethod
    def calculate_limit_tick(side: int, best_bid: float, best_ask: float,
                             tick_step: float, slippage: float = 0.05) -> int:
        """
        Calculates the Limit Tick for an order with slippage.
        Works correctly for both positive and negative rates.

        Side 0 (LONG/BUY): willing to accept higher rate → target = best_ask + |best_ask| * slippage
        Side 1 (SHORT/SELL): willing to accept lower rate → target = best_bid - |best_bid| * slippage
        """
        if side == 0:
            target_rate = best_ask + abs(best_ask) * slippage
        else:
            target_rate = best_bid - abs(best_bid) * slippage
        return PricingEngine.rate_to_tick(target_rate, tick_step)
