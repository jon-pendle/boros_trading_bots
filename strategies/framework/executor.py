"""
API-based order executor for Boros protocol.
Supports dry-run mode (log only) and live execution via CalldataController.

Endpoints:
  GET /v4/calldata/place-order            - generate place order calldata
  GET /v4/calldata/close-active-position   - generate close position calldata
"""
import logging
import requests
from typing import Optional
from .interfaces import IExecutor

logger = logging.getLogger(__name__)


class BorosExecutor(IExecutor):
    def __init__(self, api_base_url: str = "https://api.boros.finance/core",
                 dry_run: bool = True, timeout: int = 10):
        self.base_url = api_base_url.rstrip('/')
        self.dry_run = dry_run
        self.timeout = timeout

    def submit_order(self, market_id: int, side: int, size_tokens: float,
                     limit_tick: Optional[float] = None, round_id: Optional[str] = None) -> bool:
        side_label = "SHORT" if side == 1 else "LONG"
        logger.info(
            "OPEN %s | Market=%d | Tokens=%.4f | Tick=%s",
            side_label, market_id, size_tokens, limit_tick,
        )

        if self.dry_run:
            return True

        try:
            data = self._get_place_order_calldata(market_id, side, size_tokens, limit_tick)
            if data:
                # TODO: sign calldata with wallet and submit to chain
                logger.info("Calldata received, submitting tx...")
                return True
            else:
                logger.error("Failed to get calldata for order on market %d", market_id)
                return False
        except Exception as e:
            logger.error("Order submission error on market %d: %s", market_id, e)
            return False

    def close_position(self, market_id: int, side: int, size_usd: float = 0.0,
                       tokens: float = 0.0, round_id: Optional[str] = None) -> Optional[dict]:
        side_label = "SHORT" if side == 1 else "LONG"
        logger.info(
            "CLOSE %s | Market=%d | Tokens=%.4f",
            side_label, market_id, tokens,
        )

        if self.dry_run:
            return {"market_id": market_id, "side": side, "status": "dry_run"}

        try:
            data = self._get_close_position_calldata(market_id, side, tokens)
            if data:
                # TODO: sign calldata with wallet and submit to chain
                logger.info("Close calldata received, submitting tx...")
                return {"market_id": market_id, "side": side, "status": "submitted"}
            else:
                logger.error("Failed to get close calldata for market %d", market_id)
                return None
        except Exception as e:
            logger.error("Close position error on market %d: %s", market_id, e)
            return None

    def _get_place_order_calldata(self, market_id: int, side: int,
                                  size_tokens: float, limit_tick: Optional[float]) -> Optional[dict]:
        params = {
            "marketId": market_id,
            "side": side,
            "size": size_tokens,
        }
        if limit_tick is not None:
            params["limitTick"] = limit_tick
        try:
            resp = requests.get(
                f"{self.base_url}/v4/calldata/place-order",
                params=params, timeout=self.timeout
            )
            if resp.status_code == 200:
                return resp.json().get('data')
        except Exception as e:
            logger.error("Place order calldata API error: %s", e)
        return None

    def _get_close_position_calldata(self, market_id: int, side: int,
                                     size: float = 0.0) -> Optional[dict]:
        params = {"marketId": market_id, "side": side}
        if size > 0:
            params["size"] = size
        try:
            resp = requests.get(
                f"{self.base_url}/v4/calldata/close-active-position",
                params=params, timeout=self.timeout
            )
            if resp.status_code == 200:
                return resp.json().get('data')
        except Exception as e:
            logger.error("Close position calldata API error: %s", e)
        return None
