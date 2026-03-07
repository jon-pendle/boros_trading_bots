"""
V-Shape Reversal Strategy

Trigger: Oracle Funding Rate drops below entry threshold (deeply negative).
Exit: Funding Rate recovers above exit threshold AND minimum hold period elapsed.
Direction: SHORT YU (side=1) - receives fixed, pays floating.
"""
import logging
from datetime import datetime
from strategies.framework.base_strategy import BaseStrategy
from strategies.framework.interfaces import IContext
from strategies.framework.pricing import PricingEngine
from strategies.v_shape.capacity import get_depth_constrained_size
import strategies.v_shape.config as config

logger = logging.getLogger(__name__)

# Refresh market list every N ticks
MARKET_REFRESH_INTERVAL = 60


class VShapeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="v_shape", target_markets=[])
        self._markets_initialized = config.TARGET_MARKETS is not None
        if config.TARGET_MARKETS is not None:
            self.target_markets = list(config.TARGET_MARKETS.values())
            self.id_to_symbol = {v: k for k, v in config.TARGET_MARKETS.items()}
        else:
            self.id_to_symbol = {}
        self._tick_since_refresh = 0

    def _ensure_markets(self, context: IContext):
        self._tick_since_refresh += 1
        if self._markets_initialized and self._tick_since_refresh < MARKET_REFRESH_INTERVAL:
            return
        all_ids = context.data.get_all_market_ids()
        if all_ids:
            self.target_markets = all_ids
            self.id_to_symbol = {mid: context.data.get_market_name(mid) for mid in all_ids}
            self._markets_initialized = True
            self._tick_since_refresh = 0

    def on_tick(self, context: IContext) -> list[dict]:
        """Returns list of structured events for logging."""
        self._ensure_markets(context)
        events = []

        for market_id in self.target_markets:
            symbol = self.id_to_symbol.get(market_id, str(market_id))
            funding_rate = context.data.get_oracle_funding_rate(market_id)
            position = context.state.get_position(self.name, market_id)

            # Skip market if funding rate unavailable (API failure)
            if funding_rate is None:
                logger.warning("Funding rate unavailable for market %d, skipping", market_id)
                events.append({
                    "type": "skip",
                    "market_id": market_id,
                    "symbol": symbol,
                    "funding_rate": None,
                    "reason": "data_unavailable",
                })
                continue

            # Collect market snapshot
            info = context.data.get_market_info(market_id)
            market_data = info.get('data', {})
            im_data = info.get('imData', {})

            scan = {
                "type": "scan",
                "market_id": market_id,
                "symbol": symbol,
                "funding_rate": funding_rate,
                "mark_apr": float(market_data.get('markApr', 0)),
                "best_bid": float(market_data.get('bestBid', 0)),
                "best_ask": float(market_data.get('bestAsk', 0)),
                "spot_price": float(market_data.get('assetMarkPrice', 0)),
                "has_position": position is not None,
            }
            events.append(scan)

            # --- EXIT LOGIC ---
            if position:
                entry_time = datetime.fromisoformat(position['entry_time'])
                duration_hours = (context.now - entry_time).total_seconds() / 3600

                if funding_rate > config.EXIT_THRESHOLD and duration_hours >= config.MIN_HOLD_HOURS:
                    size_usd = position.get('position_size', config.POSITION_SIZE_USD)
                    tokens = position.get('tokens', 0.0)
                    round_id = position.get('round_id')

                    result = context.executor.close_position(
                        market_id, side=1, size_usd=size_usd,
                        tokens=tokens, round_id=round_id,
                    )
                    if result:
                        context.state.clear_position(self.name, market_id)
                        events.append({
                            "type": "exit",
                            "market_id": market_id,
                            "symbol": symbol,
                            "funding_rate": funding_rate,
                            "entry_rate": position.get('entry_rate'),
                            "duration_hours": round(duration_hours, 2),
                            "position_size": size_usd,
                            "tokens": tokens,
                            "round_id": round_id,
                        })
                    else:
                        events.append({
                            "type": "exit_failed",
                            "market_id": market_id,
                            "symbol": symbol,
                            "funding_rate": funding_rate,
                            "entry_rate": position.get('entry_rate'),
                            "duration_hours": round(duration_hours, 2),
                        })
                else:
                    hold_event = {
                        "type": "hold",
                        "market_id": market_id,
                        "symbol": symbol,
                        "funding_rate": funding_rate,
                        "entry_rate": position.get('entry_rate'),
                        "duration_hours": round(duration_hours, 2),
                        "position_size": position.get('position_size'),
                    }
                    # Prolonged hold warning
                    if duration_hours >= config.PROLONGED_HOLD_HOURS:
                        hold_event["prolonged"] = True
                    events.append(hold_event)
                continue

            # --- ENTRY LOGIC ---
            if funding_rate >= config.ENTRY_THRESHOLD:
                continue

            open_positions = context.state.get_all_positions(self.name)
            if len(open_positions) >= config.MAX_POSITIONS:
                events.append({
                    "type": "skip",
                    "market_id": market_id,
                    "symbol": symbol,
                    "funding_rate": funding_rate,
                    "reason": "max_positions",
                })
                continue

            # Fetch orderbook once, pass to both depth check and order pricing
            book = context.data.get_orderbook(market_id)

            position_size = get_depth_constrained_size(
                context, market_id, side=1,
                base_size=config.POSITION_SIZE_USD,
                min_depth_multiplier=config.MIN_DEPTH_MULTIPLIER,
                depth_usage_pct=config.DEPTH_USAGE_PCT,
                orderbook=book,
            )
            if position_size < 100:
                events.append({
                    "type": "skip",
                    "market_id": market_id,
                    "symbol": symbol,
                    "funding_rate": funding_rate,
                    "reason": "insufficient_depth",
                    "depth_safe_usd": round(position_size, 2),
                })
                continue

            bids = book.get('bids', [])
            asks = book.get('asks', [])
            if not bids:
                continue
            best_bid = bids[0][0]
            best_ask = asks[0][0] if asks else best_bid

            tick_step = float(im_data.get('tickStep', 1))
            limit_tick = PricingEngine.calculate_limit_tick(
                side=1, best_bid=best_bid, best_ask=best_ask, tick_step=tick_step
            )

            spot_price = context.data.get_spot_price(market_id)
            if spot_price is None:
                continue
            maturity = int(im_data.get('maturity', 0))
            now_ts = context.now.timestamp()
            time_remaining_years = max(0.001, (maturity - now_ts) / 31536000.0)
            price_per_token = abs(best_bid) * time_remaining_years * spot_price
            if price_per_token < 1e-9:
                events.append({
                    "type": "skip",
                    "market_id": market_id,
                    "symbol": symbol,
                    "funding_rate": funding_rate,
                    "reason": "zero_price_per_token",
                })
                continue
            size_tokens = position_size / price_per_token

            round_id = f"v_shape_{market_id}_{context.now.strftime('%Y%m%d_%H%M%S')}"
            trade = context.executor.submit_order(
                market_id, side=1, size_tokens=size_tokens,
                limit_tick=limit_tick, round_id=round_id
            )
            if trade:
                context.state.set_position(self.name, market_id, {
                    "entry_time": context.now.isoformat(),
                    "entry_rate": funding_rate,
                    "position_size": position_size,
                    "tokens": size_tokens,
                    "round_id": round_id,
                })
                events.append({
                    "type": "entry",
                    "market_id": market_id,
                    "symbol": symbol,
                    "funding_rate": funding_rate,
                    "position_size": round(position_size, 2),
                    "tokens": round(size_tokens, 4),
                    "limit_tick": limit_tick,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spot_price": spot_price,
                    "round_id": round_id,
                })

        return events
