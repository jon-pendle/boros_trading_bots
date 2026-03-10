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
import re
import time
import requests
from itertools import combinations
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

    def generate_pairs(self, allowed_token_ids: set = None) -> Dict[str, tuple[int, int]]:
        """
        Dynamically generate arbitrage pairs from all active markets.
        Groups by (base_asset, maturity, tokenId) — only markets sharing
        the same collateral pool are paired together.
        Symbol format: PLATFORM-TICKER-EXPIRY (e.g. BINANCE-BTCUSDT-27MAR2026)

        Args:
            allowed_token_ids: if set, only include markets with these tokenIds
        """
        now = int(time.time())
        # Ensure market cache is populated
        if not self._market_cache:
            self.get_all_market_ids()

        # Group active markets by (base_asset, maturity, tokenId)
        groups: Dict[tuple, list[int]] = {}
        for mid, info in self._market_cache.items():
            state = info.get('state', '')
            if state != 'Normal':
                continue
            im_data = info.get('imData', {})
            maturity = int(im_data.get('maturity', 0))
            if maturity <= now:
                continue
            token_id = int(info.get('tokenId', 0))
            if allowed_token_ids and token_id not in allowed_token_ids:
                continue
            symbol = im_data.get('symbol', '')
            base = self._parse_base_asset(symbol)
            if not base:
                continue
            key = (base, maturity, token_id)
            groups.setdefault(key, []).append(mid)

        # Generate all pairs within each group
        pairs = {}
        for (base, maturity, token_id), ids in groups.items():
            if len(ids) < 2:
                continue
            for id_a, id_b in combinations(sorted(ids), 2):
                label = f"{base}_{id_a}_{id_b}"
                pairs[label] = (id_a, id_b)

        logger.info("Generated %d pairs from %d groups (%d markets)",
                     len(pairs), len(groups), len(self._market_cache))
        return pairs

    @staticmethod
    def _parse_base_asset(symbol: str) -> Optional[str]:
        """Extract normalized base asset from symbol like BINANCE-BTCUSDT-27MAR2026."""
        if not symbol:
            return None
        parts = symbol.split('-')
        if len(parts) < 3:
            return None
        raw_ticker = parts[-2].upper()
        # Strip USDT/USDC/USD suffix; handle xyz prefix (xyzGOLD -> GOLD)
        base = re.sub(r'^XYZ', '', raw_ticker)
        base = re.sub(r'(USDT|USDC|USD)$', '', base)
        # Map XAU -> GOLD for consistency
        if base == 'XAU':
            base = 'GOLD'
        return base if base else None

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

    # ------------------------------------------------------------------
    # Collateral / Margin
    # ------------------------------------------------------------------

    def get_trade_history(self, user_address: str, market_id: int,
                          account_id: int = 0,
                          limit: int = 50) -> list[dict]:
        """
        GET /v1/pnl/transactions -> trade history for a market.
        Returns list of trades sorted by time ascending.
        Each trade: {marketId, time (unix), side, notionalSize, fixedApr, ...}
        """
        data = self._get(
            "/v1/pnl/transactions",
            params={
                "userAddress": user_address,
                "accountId": account_id,
                "marketId": market_id,
                "limit": limit,
            },
        )
        if not data:
            return []
        results = data if isinstance(data, list) else data.get("results", [])
        results.sort(key=lambda t: t.get("time", 0))
        return results

    def get_collateral_summary(self, user_address: str,
                               account_id: int = 0) -> Dict[int, float]:
        """
        GET /v1/collaterals/summary -> available balance per tokenId.
        Returns {tokenId: availableBalance} mapping (for margin pre-check).
        """
        detailed = self.get_collateral_detail(user_address, account_id)
        return {tid: info["available"] for tid, info in detailed.items()}

    def get_collateral_detail(self, user_address: str,
                              account_id: int = 0) -> Dict[int, Dict[str, Any]]:
        """
        GET /v1/collaterals/summary -> detailed collateral info per tokenId.
        Returns {tokenId: {available, net_balance, initial_margin, maint_margin,
                           margin_ratio, positions: [{marketId, size, side, ...}]}}
        """
        data = self._get(
            "/v1/collaterals/summary",
            params={"userAddress": user_address, "accountId": account_id},
        )
        if not data:
            return {}
        results = data.get("collaterals", [])
        detail = {}
        for item in results:
            tid = int(item.get("tokenId", 0))
            if tid <= 0:
                continue
            cross = item.get("crossPosition", {})

            def _wei(val):
                try:
                    return int(val) / 1e18
                except (ValueError, TypeError):
                    return float(val) if val else 0.0

            positions = []
            for mp in cross.get("marketPositions", []):
                # Size from notionalSize (wei string), side from int (0=long,1=short)
                notional_raw = mp.get("notionalSize", "0")
                notional = _wei(notional_raw)
                try:
                    notional_wei = str(abs(int(notional_raw)))
                except (ValueError, TypeError):
                    notional_wei = ""
                side_int = int(mp.get("side", 0))
                # Rates: fixedApr = entry rate, markApr = current rate
                entry_rate = float(mp.get("fixedApr", 0))
                mark_rate = float(mp.get("markApr", 0))
                pnl = mp.get("pnl", {})
                unrealized = _wei(pnl.get("unrealisedPnl", "0"))
                positions.append({
                    "market_id": int(mp.get("marketId", 0)),
                    "size": abs(notional),
                    "size_wei": notional_wei,
                    "side": side_int,
                    "entry_rate": entry_rate,
                    "mark_rate": mark_rate,
                    "unrealized_pnl": unrealized,
                    "token_id": tid,
                })

            detail[tid] = {
                "available": _wei(cross.get("availableBalance", "0")),
                "net_balance": _wei(cross.get("netBalance",
                                              item.get("totalNetBalance", "0"))),
                "initial_margin": _wei(cross.get("initialMargin", "0")),
                "maint_margin": _wei(cross.get("maintMargin", "0")),
                "margin_ratio": float(cross.get("marginRatio", 0)),
                "positions": positions,
            }
        return detail
