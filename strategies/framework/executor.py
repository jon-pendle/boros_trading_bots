"""
API-based order executor for Boros protocol.
Supports dry-run mode (log only) and live execution via agent signing.

Flow (live mode):
  1. Generate calldata via Boros API
  2. Sign with agent key (EIP-712)
  3. Submit to POST /v2/agent/bulk-direct-call

Endpoints:
  Core API (https://api.boros.finance/core):
    GET  /v4/calldata/place-order               - single market order calldata
    POST /v1/calldata/dual-market-place-order   - dual market atomic order calldata
    GET  /v4/calldata/close-active-position     - close position calldata
  Send-Txs-Bot API (https://api.boros.finance/send-txs-bot):
    POST /v2/agent/bulk-direct-call             - submit signed transactions
"""
import logging
import requests
from typing import Optional
from .interfaces import IExecutor

logger = logging.getLogger(__name__)

SEND_TXS_BOT_URL = "https://api.boros.finance/send-txs-bot"


MAX_RETRIES = 3
RETRY_BACKOFF = 1.0  # seconds: 1, 2, 4
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class BorosExecutor(IExecutor):
    def __init__(self, api_base_url: str = "https://api.boros.finance/core",
                 dry_run: bool = True, timeout: int = 10,
                 signer=None, data_provider=None):
        """
        Args:
            signer: AgentSigner instance (required for live mode).
            data_provider: BorosDataProvider for market info lookup (tokenId).
        """
        self.base_url = api_base_url.rstrip('/')
        self.dry_run = dry_run
        self.timeout = timeout
        self.signer = signer
        self.data_provider = data_provider

    def _request_with_retry(self, method: str, url: str,
                            params=None, json=None,
                            timeout=None) -> Optional[requests.Response]:
        """HTTP request with retry + exponential backoff for transient failures."""
        import time as _time
        timeout = timeout or self.timeout
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.request(method, url, params=params, json=json,
                                        timeout=timeout)
                if resp.status_code in (200, 201):
                    return resp
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    logger.warning("%s %s -> HTTP %d (attempt %d/%d)",
                                   method, url, resp.status_code,
                                   attempt + 1, MAX_RETRIES)
                else:
                    # Non-retryable (400, 403, 404, etc.) — return immediately
                    return resp
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                logger.warning("%s %s -> %s (attempt %d/%d)",
                               method, url, type(e).__name__,
                               attempt + 1, MAX_RETRIES)
            except Exception as e:
                logger.error("%s %s unexpected error: %s", method, url, e)
                return None
            if attempt < MAX_RETRIES - 1:
                _time.sleep(RETRY_BACKOFF * (2 ** attempt))
        logger.error("%s %s failed after %d retries", method, url, MAX_RETRIES)
        return None

    def _get_market_acc(self, market_id: int) -> Optional[str]:
        """Derive cross-margin marketAcc for a given market."""
        if not self.signer or not self.data_provider:
            return None
        from .signing import derive_cross_market_acc
        info = self.data_provider.get_market_info(market_id)
        token_id = int(info.get('tokenId', 0))
        if token_id <= 0:
            logger.error("Cannot derive marketAcc: unknown tokenId for market %d", market_id)
            return None
        return derive_cross_market_acc(
            self.signer.root_address, token_id, account_id=0)

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
            calldatas = self._get_place_order_calldata(market_id, side, size_tokens, limit_tick)
            if not calldatas:
                logger.error("Failed to get calldata for order on market %d", market_id)
                return False

            result = self._sign_and_submit(calldatas)
            if result:
                logger.info("Order submitted on market %d: %s", market_id, "ok")
                return True
            return False
        except Exception as e:
            logger.error("Order submission error on market %d: %s", market_id, e)
            return False

    def close_position(self, market_id: int, side: int, size_usd: float = 0.0,
                       tokens: float = 0.0, tokens_wei: str = "",
                       round_id: Optional[str] = None) -> Optional[dict]:
        side_label = "SHORT" if side == 1 else "LONG"
        logger.info(
            "CLOSE %s | Market=%d | Tokens=%.4f",
            side_label, market_id, tokens,
        )

        if self.dry_run:
            return {"market_id": market_id, "side": side, "status": "dry_run"}

        try:
            calldatas = self._get_close_position_calldata(
                market_id, side, tokens, tokens_wei=tokens_wei)
            if not calldatas:
                logger.error("Failed to get close calldata for market %d", market_id)
                return None

            result = self._sign_and_submit(calldatas)
            if result:
                logger.info("Close submitted on market %d: %s", market_id, "ok")
                return {"market_id": market_id, "side": side, "status": "submitted"}
            return None
        except Exception as e:
            logger.error("Close position error on market %d: %s", market_id, e)
            return None

    def submit_dual_order(self, mkt_a: int, side_a: int, mkt_b: int, side_b: int,
                          size_tokens: float, limit_tick_a: Optional[float] = None,
                          limit_tick_b: Optional[float] = None,
                          round_id: Optional[str] = None) -> bool:
        """
        Atomic dual-market entry: both legs succeed or both fail.
        Uses POST /v1/calldata/dual-market-place-order + bulk-direct-call(requireSuccess=true).
        """
        side_a_label = "SHORT" if side_a == 1 else "LONG"
        side_b_label = "SHORT" if side_b == 1 else "LONG"
        logger.info(
            "DUAL OPEN | A: %s mkt=%d | B: %s mkt=%d | Tokens=%.4f",
            side_a_label, mkt_a, side_b_label, mkt_b, size_tokens,
        )

        if self.dry_run:
            return True

        try:
            calldatas = self._get_dual_place_order_calldata(
                mkt_a, side_a, mkt_b, side_b, size_tokens,
                limit_tick_a, limit_tick_b)
            if not calldatas:
                logger.error("Failed to get dual calldata for markets %d/%d", mkt_a, mkt_b)
                return False

            result = self._sign_and_submit(calldatas)
            if result:
                logger.info("Dual order submitted: markets %d/%d", mkt_a, mkt_b)
                return True
            return False
        except Exception as e:
            logger.error("Dual order error markets %d/%d: %s", mkt_a, mkt_b, e)
            return False

    def close_dual_position(self, mkt_a: int, side_a: int, tokens_a: float,
                            mkt_b: int, side_b: int, tokens_b: float,
                            tokens_wei_a: str = "", tokens_wei_b: str = "",
                            round_id: Optional[str] = None) -> Optional[dict]:
        """
        Atomic dual close: both legs succeed or both fail.
        Gets close calldatas for each leg, then submits together via bulk-direct-call.
        """
        logger.info(
            "DUAL CLOSE | A: mkt=%d tokens=%.4f | B: mkt=%d tokens=%.4f",
            mkt_a, tokens_a, mkt_b, tokens_b,
        )

        if self.dry_run:
            return {"market_a": mkt_a, "market_b": mkt_b, "status": "dry_run"}

        try:
            calldatas_a = self._get_close_position_calldata(
                mkt_a, side_a, tokens_a, tokens_wei=tokens_wei_a)
            calldatas_b = self._get_close_position_calldata(
                mkt_b, side_b, tokens_b, tokens_wei=tokens_wei_b)

            if not calldatas_a or not calldatas_b:
                logger.error("Failed to get close calldatas: A=%s B=%s",
                             bool(calldatas_a), bool(calldatas_b))
                return None

            # Merge both legs into one atomic submission
            all_calldatas = calldatas_a + calldatas_b
            result = self._sign_and_submit(all_calldatas)
            if result:
                logger.info("Dual close submitted: markets %d/%d", mkt_a, mkt_b)
                return {"market_a": mkt_a, "market_b": mkt_b, "status": "submitted"}
            return None
        except Exception as e:
            logger.error("Dual close error markets %d/%d: %s", mkt_a, mkt_b, e)
            return None

    def _sign_and_submit(self, calldatas: list[str]) -> Optional[dict]:
        """Sign calldatas with agent key and submit to Boros."""
        if not self.signer:
            logger.error("No signer configured for live execution")
            return None

        signed_datas = self.signer.sign_calldatas(calldatas)

        import json as _json
        for i, d in enumerate(signed_datas):
            logger.info(
                "signed_data[%d]: agent=%s nonce=%s connId=%s account=%s sig=%s.. calldata=%s..",
                i, d["agent"],
                d["message"]["nonce"],
                d["message"]["connectionId"][:18],
                d["message"]["account"][:18],
                d["signature"][:18],
                d["calldata"][:18],
            )

        payload = {
            "datas": signed_datas,
            "skipReceipt": False,
        }
        logger.info("bulk-direct-call payload: %s", _json.dumps(payload)[:500])

        try:
            resp = self._request_with_retry(
                "POST", f"{SEND_TXS_BOT_URL}/v2/agent/bulk-direct-call",
                json=payload, timeout=30,
            )
            if resp is None:
                return None
            if resp.status_code in (200, 201):
                data = resp.json()
                logger.info("bulk-direct-call response: %s",
                            _json.dumps(data)[:500] if isinstance(data, (dict, list)) else str(data)[:500])
                # Verify transactions succeeded on-chain
                # enterMarket reverts are non-fatal (market already entered)
                if isinstance(data, list):
                    for d in data:
                        status = d.get("status", "")
                        error = d.get("error", "")
                        if status != "success":
                            if "MMMarketNotEntered" in error or "MarketAlreadyEntered" in error:
                                logger.info(
                                    "Ignoring benign revert: index=%s error=%s",
                                    d.get("index"), error,
                                )
                                continue
                            logger.error(
                                "Tx not successful: index=%s status=%s error=%s txHash=%s",
                                d.get("index"), status, error, d.get("txHash", ""),
                            )
                            return None
                return data
            else:
                logger.error(
                    "bulk-direct-call failed: HTTP %d - %s",
                    resp.status_code, resp.text[:200],
                )
                return None
        except Exception as e:
            logger.error("Transaction submission error: %s", e)
            return None

    def _get_dual_place_order_calldata(self, mkt_a: int, side_a: int,
                                        mkt_b: int, side_b: int,
                                        size_tokens: float,
                                        limit_tick_a: Optional[float],
                                        limit_tick_b: Optional[float]) -> Optional[list[str]]:
        """Get calldata via POST /v1/calldata/dual-market-place-order."""
        market_acc = self._get_market_acc(mkt_a)
        if not market_acc:
            logger.error("Cannot derive marketAcc for dual order")
            return None

        size_wei = str(int(size_tokens * 1e18))

        order1 = {
            "marketId": mkt_a,
            "side": side_a,
            "size": size_wei,
            "tif": 2,  # FOK
            "slippage": 0.05,
        }
        if limit_tick_a is not None:
            order1["limitTick"] = int(limit_tick_a)

        order2 = {
            "marketId": mkt_b,
            "side": side_b,
            "size": size_wei,
            "tif": 2,  # FOK
            "slippage": 0.05,
        }
        if limit_tick_b is not None:
            order2["limitTick"] = int(limit_tick_b)

        body = {
            "marketAcc": market_acc,
            "order1": order1,
            "order2": order2,
        }

        try:
            resp = self._request_with_retry(
                "POST", f"{self.base_url}/v1/calldata/dual-market-place-order",
                json=body,
            )
            if resp is None:
                return None
            if resp.status_code in (200, 201):
                data = resp.json()
                calldatas = data.get('calldatas', [])
                if not calldatas:
                    cd = data.get('data')
                    if cd:
                        calldatas = [cd] if isinstance(cd, str) else cd
                return calldatas if calldatas else None
            else:
                logger.error("Dual place order calldata: HTTP %d - %s",
                             resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Dual place order calldata API error: %s", e)
        return None

    def _get_place_order_calldata(self, market_id: int, side: int,
                                  size_tokens: float, limit_tick: Optional[float]) -> Optional[list[str]]:
        params = {
            "marketId": market_id,
            "side": side,
            "size": str(int(size_tokens * 1e18)),  # Convert to wei string
            "tif": 2,  # FILL_OR_KILL: full fill or cancel (no partial)
            "slippage": 0.05,  # 5% price protection (required for market orders)
        }
        if limit_tick is not None:
            params["limitTick"] = int(limit_tick)
        market_acc = self._get_market_acc(market_id)
        if market_acc:
            params["marketAcc"] = market_acc

        try:
            resp = self._request_with_retry(
                "GET", f"{self.base_url}/v4/calldata/place-order",
                params=params,
            )
            if resp is None:
                return None
            if resp.status_code == 200:
                data = resp.json()
                calldatas = data.get('calldatas', [])
                if not calldatas:
                    cd = data.get('data')
                    if cd:
                        calldatas = [cd] if isinstance(cd, str) else cd
                return calldatas if calldatas else None
            else:
                logger.error("Place order calldata: HTTP %d - %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Place order calldata API error: %s", e)
        return None

    def _get_close_position_calldata(self, market_id: int, side: int,
                                     size: float = 0.0,
                                     tokens_wei: str = "") -> Optional[list[str]]:
        # close-active-position 'side' = closing order direction (opposite of position side)
        # SHORT position (1) -> close by going LONG (0)
        # LONG position (0) -> close by going SHORT (1)
        close_side = 1 - int(side)
        params = {
            "marketId": market_id,
            "side": close_side,
            "tif": 2,  # FILL_OR_KILL
            "slippage": 0.05,  # 5% price protection
        }
        if tokens_wei:
            # Use exact wei value — no float precision loss
            params["size"] = tokens_wei
        elif size > 0:
            params["size"] = str(int(size * 1e18))
        market_acc = self._get_market_acc(market_id)
        if market_acc:
            params["marketAcc"] = market_acc

        logger.info("Close calldata params: %s", params)

        try:
            resp = self._request_with_retry(
                "GET", f"{self.base_url}/v4/calldata/close-active-position",
                params=params,
            )
            if resp is None:
                return None
            if resp.status_code == 200:
                data = resp.json()
                calldatas = data.get('calldatas', [])
                if not calldatas:
                    cd = data.get('data')
                    if cd:
                        calldatas = [cd] if isinstance(cd, str) else cd
                return calldatas if calldatas else None
            else:
                logger.error("Close calldata: HTTP %d - %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Close position calldata API error: %s", e)
        return None
