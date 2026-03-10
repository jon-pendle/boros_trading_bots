import math


class PricingEngine:
    @staticmethod
    def rate_to_tick(rate: float, tick_step: float) -> int:
        """Converts a floating point rate into the integer Tick index used by the protocol."""
        if tick_step == 0:
            return 0
        return math.floor(rate / tick_step)

    @staticmethod
    def calculate_im_per_token(rate: float, k_im: float, t_thresh_seconds: float,
                               i_tick_thresh: int, tick_step: int,
                               margin_floor: float,
                               time_to_maturity_seconds: float) -> float:
        """
        Boros Initial Margin per token.

        Formula: IM = |Size| × max(|Rate|, RateFloor) × kIM × max(TTM_y, tThresh_y)
        where RateFloor = 1.00005^(iTickThresh × tickStep) - 1
              TTM_y = time_to_maturity / 31536000
              tThresh_y = tThresh / 31536000

        Returns IM per 1 token (in base asset units).
        Multiply by spot_price to get USD-equivalent.
        """
        SECONDS_PER_YEAR = 31_536_000
        rate_floor = 1.00005 ** (i_tick_thresh * tick_step) - 1
        rate_factor = max(abs(rate), rate_floor)
        ttm_years = max(time_to_maturity_seconds / SECONDS_PER_YEAR,
                        t_thresh_seconds / SECONDS_PER_YEAR)
        im_per_token = rate_factor * k_im * ttm_years
        return max(im_per_token, margin_floor)

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
