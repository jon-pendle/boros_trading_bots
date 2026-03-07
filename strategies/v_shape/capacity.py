"""
Capacity calculation based on orderbook depth.
Ensures position sizing doesn't exceed available liquidity.

In Boros IRS, orderbook levels are (rate, tokens) where rate is APR.
The USD value of tokens = tokens * |rate| * time_remaining_years * spot_price.
"""
from typing import Optional
from strategies.framework.interfaces import IContext


def calculate_available_depth(orderbook: dict, side: int,
                              spot_price: float, time_remaining_years: float,
                              n_levels: int = 10) -> tuple[float, float]:
    """
    Calculate available liquidity depth from top N levels of the orderbook.

    For SHORT (side=1): we hit bids (sell into bids).
    For LONG (side=0): we hit asks (buy from asks).

    Returns:
        (total_tokens, total_usd): Available depth in tokens and USD notional.
    """
    levels = orderbook.get('bids' if side == 1 else 'asks', [])
    if not levels:
        return 0.0, 0.0

    total_tokens = 0.0
    total_usd = 0.0

    for i, (rate, size_tokens) in enumerate(levels):
        if i >= n_levels:
            break
        total_tokens += size_tokens
        total_usd += size_tokens * abs(rate) * time_remaining_years * spot_price

    return total_tokens, total_usd


def get_depth_constrained_size(context: IContext, market_id: int, side: int,
                               base_size: float,
                               min_depth_multiplier: float = 1.0,
                               depth_usage_pct: float = 0.30,
                               orderbook: Optional[dict] = None) -> float:
    """
    Get position size constrained by market depth from the live orderbook.

    Args:
        context: Live context with data provider
        market_id: Market to check
        side: 1=SHORT, 0=LONG
        base_size: Desired position size in USD
        min_depth_multiplier: Minimum depth required as multiple of base_size
        depth_usage_pct: Max fraction of available depth to consume
        orderbook: Pre-fetched orderbook (avoids duplicate API call)

    Returns:
        Actual position size to use (USD), or 0 if insufficient depth.
    """
    if orderbook is None:
        orderbook = context.data.get_orderbook(market_id)
    spot_price = context.data.get_spot_price(market_id)
    if spot_price is None:
        return 0.0

    info = context.data.get_market_info(market_id)
    maturity = int(info.get('imData', {}).get('maturity', 0))
    now_ts = context.now.timestamp()
    time_remaining_years = max(0.001, (maturity - now_ts) / 31536000.0)

    tokens_available, usd_available = calculate_available_depth(
        orderbook, side, spot_price, time_remaining_years, n_levels=10
    )

    min_required_depth = base_size * min_depth_multiplier
    if usd_available < min_required_depth:
        return 0.0

    max_safe_size = usd_available * depth_usage_pct
    return min(base_size, max_safe_size)
