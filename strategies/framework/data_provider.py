"""
API-based data provider for Boros protocol.
All market data is fetched from the Boros REST API.

Base URL: https://api.boros.finance/core
Endpoints:
  GET /v1/markets                          - list all markets
  GET /v1/markets/{marketId}               - single market info
  GET /v1/order-books/{marketId}           - orderbook (merged AMM + LOB)
  GET /v2/markets/indicators               - funding rates, mark APR, etc.

Orderbook response format:
  ia = tick indices (rate = ia * tickSize)
  sz = size in wei (18 decimals, bigint strings)
  long side = bids (buyers of yield), short side = asks (sellers of yield)

Market data fields (from /v1/markets response):
  data.assetMarkPrice = spot price of underlying asset (e.g. BTC price)
  data.bestBid / data.bestAsk = best bid/ask APR
  data.markApr = current mark APR
  data.floatingApr = current floating/oracle APR
"""
import logging
import time
import requests
from typing import Dict, Any, Optional
from .interfaces import IDataProvider

logger = logging.getLogger(__name__)

DEFAULT_OB_TICK_SIZE = 0.001
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds: 1, 2, 4
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class BorosDataProvider(IDataProvider):
    def __init__(self, api_base_url: str = "https://api.boros.finance/core",
                 timeout: int = 10):
        self.base_url = api_base_url.rstrip('/')
        self.timeout = timeout
        self._market_cache: Dict[int, Dict[str, Any]] = {}

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code in RETRYABLE_STATUS_CODES:
                    last_error = f"HTTP {resp.status_code}"
                    logger.warning(
                        "GET %s -> %s (attempt %d/%d)",
                        path, last_error, attempt + 1, MAX_RETRIES,
                    )
                else:
                    # Non-retryable HTTP error
                    logger.error("GET %s -> HTTP %d (not retrying)", path, resp.status_code)
                    return None
            except requests.exceptions.Timeout:
                last_error = "timeout"
                logger.warning(
                    "GET %s timed out (attempt %d/%d)", path, attempt + 1, MAX_RETRIES,
                )
            except requests.exceptions.ConnectionError as e:
                last_error = str(e)
                logger.warning(
                    "GET %s connection error (attempt %d/%d): %s",
                    path, attempt + 1, MAX_RETRIES, e,
                )
            except Exception as e:
                logger.error("GET %s unexpected error: %s", path, e)
                return None

            if attempt < MAX_RETRIES - 1:
                sleep_time = RETRY_BACKOFF_BASE * (2 ** attempt)
                time.sleep(sleep_time)

        logger.error("GET %s failed after %d retries: %s", path, MAX_RETRIES, last_error)
        return None

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_all_market_ids(self) -> list[int]:
        """GET /v1/markets -> {results: [{marketId, ...}, ...]}"""
        data = self._get("/v1/markets")
        if data:
            markets = data.get('results', [])
            ids = [int(m['marketId']) for m in markets if 'marketId' in m]
            # Cache all market data while we have it
            for m in markets:
                mid = int(m['marketId'])
                self._market_cache[mid] = m
            return ids
        return []

    def get_market_info(self, market_id: int) -> Dict[str, Any]:
        """
        Returns market info. Tries cache first (populated by get_all_market_ids).
        Structure: {marketId, imData: {name, symbol, maturity, tickStep, ...},
                    config: {...}, metadata: {...}, data: {bestBid, bestAsk, markApr, ...}}
        """
        if market_id in self._market_cache:
            return self._market_cache[market_id]

        data = self._get(f"/v1/markets/{market_id}")
        if data:
            self._market_cache[market_id] = data
            return data
        return {}

    # ------------------------------------------------------------------
    # Indicators / Funding
    # ------------------------------------------------------------------

    def get_oracle_funding_rate(self, market_id: int) -> Optional[float]:
        """
        Underlying APR ('u') from indicators. This is the settlement/oracle rate.
        GET /v2/markets/indicators?marketId=X&select=u&timeFrame=5m
        Returns None if API call fails (distinguishes from real 0.0).
        """
        data = self._get(
            "/v2/markets/indicators",
            params={"marketId": market_id, "select": "u", "timeFrame": "5m"}
        )
        if data:
            results = data.get('results', [])
            if results:
                return float(results[-1].get('u', 0))
            # API succeeded but no results — return 0.0 (valid response, no data)
            return 0.0
        return None

    def get_mark_apr(self, market_id: int) -> float:
        """Mark APR from cached market data (data.markApr field)."""
        info = self.get_market_info(market_id)
        return float(info.get('data', {}).get('markApr', 0))

    # ------------------------------------------------------------------
    # Spot Price
    # ------------------------------------------------------------------

    def get_spot_price(self, market_id: int) -> Optional[float]:
        """
        Returns the underlying asset's spot price (e.g. BTC price in USD).
        Uses data.assetMarkPrice from market info.
        Returns None if market info unavailable.
        """
        info = self.get_market_info(market_id)
        if not info:
            return None
        market_data = info.get('data', {})
        val = market_data.get('assetMarkPrice')
        if val is not None and float(val) > 0:
            return float(val)
        return 1.0

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    def get_orderbook(self, market_id: int,
                      tick_size: float = DEFAULT_OB_TICK_SIZE
                      ) -> dict[str, list[tuple[float, float]]]:
        """
        GET /v1/order-books/{marketId}?tickSize=X
        Response: {long: {ia: [...], sz: [...]}, short: {ia: [...], sz: [...]}}
        - long side = bids (lower rates, buyers of yield)
        - short side = asks (higher rates, sellers of yield)
        - ia = tick indices, actual rate = ia * tickSize
        - sz = size in wei (bigint strings, 18 decimals)
        """
        data = self._get(
            f"/v1/order-books/{market_id}",
            params={"tickSize": tick_size}
        )
        if not data:
            return {'bids': [], 'asks': []}

        bids = self._parse_ob_side(data.get('long', {}), tick_size)
        asks = self._parse_ob_side(data.get('short', {}), tick_size)

        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        return {'bids': bids, 'asks': asks}

    @staticmethod
    def _parse_ob_side(side_data: dict, tick_size: float) -> list[tuple[float, float]]:
        """Parse OB side from {ia: [tick_indices], sz: [wei_strings]}."""
        if not side_data:
            return []
        ia_list = side_data.get('ia', [])
        sz_list = side_data.get('sz', [])
        levels = []
        for i in range(min(len(ia_list), len(sz_list))):
            rate = float(ia_list[i]) * tick_size
            try:
                size_tokens = int(sz_list[i]) / 1e18
            except (ValueError, TypeError):
                size_tokens = float(sz_list[i])
            if size_tokens > 0:
                levels.append((rate, size_tokens))
        return levels

    # ------------------------------------------------------------------
    # Convenience helpers (extracted from market info)
    # ------------------------------------------------------------------

    def get_tick_step(self, market_id: int) -> float:
        info = self.get_market_info(market_id)
        return float(info.get('imData', {}).get('tickStep', 1))

    def get_maturity(self, market_id: int) -> int:
        info = self.get_market_info(market_id)
        return int(info.get('imData', {}).get('maturity', 0))

    def get_market_name(self, market_id: int) -> str:
        info = self.get_market_info(market_id)
        return info.get('imData', {}).get('name', str(market_id))

    def get_best_bid_ask(self, market_id: int) -> tuple[float, float]:
        """Get best bid/ask from cached market data (fast, no OB call)."""
        info = self.get_market_info(market_id)
        d = info.get('data', {})
        return float(d.get('bestBid', 0)), float(d.get('bestAsk', 0))
