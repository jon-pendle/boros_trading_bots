"""
State managers for position persistence.
- InMemoryStateManager: volatile, for testing
- JsonFileStateManager: persists to disk, survives restarts (sim mode)
- ApiStateManager: recovers state from Boros API on startup (prod mode)
"""
import json
import logging
import os
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from .interfaces import IStateManager

logger = logging.getLogger(__name__)


class InMemoryStateManager(IStateManager):
    def __init__(self):
        self._positions: Dict[str, Dict[str, Any]] = {}

    def get_position(self, strategy_name: str, market_id: int) -> Optional[Dict[str, Any]]:
        return self._positions.get(f"{strategy_name}_{market_id}")

    def get_all_positions(self, strategy_name: str) -> Dict:
        result = {}
        prefix = f"{strategy_name}_"
        for key, data in self._positions.items():
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                try:
                    pos_key = int(suffix)
                except ValueError:
                    pos_key = suffix  # preserve string keys (pair names)
                result[pos_key] = data
        return result

    def set_position(self, strategy_name: str, market_id, data: Dict[str, Any]):
        self._positions[f"{strategy_name}_{market_id}"] = data

    def clear_position(self, strategy_name: str, market_id):
        key = f"{strategy_name}_{market_id}"
        self._positions.pop(key, None)


class JsonFileStateManager(IStateManager):
    """Persists positions to a JSON file so state survives bot restarts."""

    def __init__(self, file_path: str = "bot_state.json"):
        self.file_path = file_path
        self._positions = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save(self):
        with open(self.file_path, 'w') as f:
            json.dump(self._positions, f, indent=2)

    def get_position(self, strategy_name: str, market_id) -> Optional[Dict[str, Any]]:
        return self._positions.get(f"{strategy_name}_{market_id}")

    def get_all_positions(self, strategy_name: str) -> Dict:
        result = {}
        prefix = f"{strategy_name}_"
        for key, data in self._positions.items():
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                try:
                    pos_key = int(suffix)
                except ValueError:
                    pos_key = suffix
                result[pos_key] = data
        return result

    def set_position(self, strategy_name: str, market_id: int, data: Dict[str, Any]):
        self._positions[f"{strategy_name}_{market_id}"] = data
        self._save()

    def clear_position(self, strategy_name: str, market_id: int):
        key = f"{strategy_name}_{market_id}"
        if self._positions.pop(key, None) is not None:
            self._save()


class ApiStateManager(InMemoryStateManager):
    """
    Prod state manager: recovers positions from Boros API on startup,
    then keeps state in memory with entry_time persisted to disk.

    Uses GET /v1/collaterals/summary to find existing positions.
    Entry times are saved to a small JSON file so holding hours survive restarts.
    """

    ENTRY_TIMES_FILE = "entry_times.json"

    MAX_RETRIES = 3
    RETRY_BACKOFF = 2.0  # seconds: 2, 4, 8

    def __init__(self, api_base_url: str, user_address: str, account_id: int = 0,
                 timeout: int = 10):
        super().__init__()
        self.api_base_url = api_base_url.rstrip('/')
        self.user_address = user_address
        self.account_id = account_id
        self.timeout = timeout
        self._entry_times = self._load_entry_times()

    def _api_get_with_retry(self, url: str, params: dict = None) -> Optional[dict]:
        """GET with retry + exponential backoff for transient failures."""
        import time as _time
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning("GET %s -> HTTP %d (attempt %d/%d)",
                                   url, resp.status_code, attempt + 1, self.MAX_RETRIES)
                else:
                    logger.error("GET %s -> HTTP %d (not retrying)", url, resp.status_code)
                    return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning("GET %s -> %s (attempt %d/%d)",
                               url, type(e).__name__, attempt + 1, self.MAX_RETRIES)
            except Exception as e:
                logger.error("GET %s unexpected error: %s", url, e)
                return None
            if attempt < self.MAX_RETRIES - 1:
                _time.sleep(self.RETRY_BACKOFF * (2 ** attempt))
        logger.error("GET %s failed after %d retries", url, self.MAX_RETRIES)
        return None

    def _load_entry_times(self) -> dict:
        if os.path.exists(self.ENTRY_TIMES_FILE):
            try:
                with open(self.ENTRY_TIMES_FILE, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_entry_times(self):
        with open(self.ENTRY_TIMES_FILE, 'w') as f:
            json.dump(self._entry_times, f, indent=2)

    def set_position(self, strategy_name: str, market_id: int, data: Dict[str, Any]):
        """Save position and persist entry_time."""
        super().set_position(strategy_name, market_id, data)
        key = f"{strategy_name}_{market_id}"
        entry_time = data.get("entry_time")
        if entry_time and entry_time != "recovered":
            self._entry_times[key] = entry_time
            self._save_entry_times()

    def clear_position(self, strategy_name: str, market_id: int):
        """Clear position and remove persisted entry_time."""
        super().clear_position(strategy_name, market_id)
        key = f"{strategy_name}_{market_id}"
        if self._entry_times.pop(key, None) is not None:
            self._save_entry_times()

    def get_entry_time(self, strategy_name: str, market_id: int) -> Optional[str]:
        """
        Get entry time for a position. Priority:
          1. Persisted entry_times.json (local cache)
          2. Trade history API /v1/pnl/transactions
        Returns ISO timestamp string or None.
        """
        key = f"{strategy_name}_{market_id}"
        entry_time = self._entry_times.get(key)
        if entry_time:
            return entry_time

        # Look up from trade history API
        try:
            data = self._api_get_with_retry(
                f"{self.api_base_url}/v1/pnl/transactions",
                params={
                    "userAddress": self.user_address,
                    "accountId": self.account_id,
                    "marketId": market_id,
                    "limit": 50,
                },
            )
            if data:
                trades = data if isinstance(data, list) else data.get("results", [])
                # Sort by time descending (newest first)
                # tradeDirection: 0=INCREASE, 1=DECREASE, 2=CHANGE_DIRECTION
                trades = [t for t in trades if t.get("time")]
                trades.sort(key=lambda t: t["time"], reverse=True)
                # Find current position's entry time.
                # Walk from newest to oldest looking at INCREASE trades.
                # DECREASE trades are skipped (partial close, side field
                # is the trade direction not position direction).
                # FLIP (tradeDirection=2) marks current position's start.
                # Consecutive INCREASEs with the same side belong to the
                # same position — keep the earliest one.
                entry_ts = None
                entry_side = None
                for t in trades:
                    td = t.get("tradeDirection")
                    if td == 2:  # FLIP = current position started here
                        entry_ts = t["time"]
                        break
                    if td == 1:  # DECREASE = partial/full close, skip
                        continue
                    if td == 0:  # INCREASE = open/add
                        if entry_side is None:
                            entry_side = t.get("side")
                        if t.get("side") != entry_side:
                            break  # different side = previous position
                        entry_ts = t["time"]  # keep going to find earliest
                if entry_ts:
                    entry_time = datetime.fromtimestamp(
                        entry_ts, tz=timezone.utc).isoformat()
                    logger.info("  [%d] Entry time from trade history: %s", market_id, entry_time)
                    self._entry_times[key] = entry_time
                    self._save_entry_times()
                    return entry_time
        except Exception as e:
            logger.warning("  [%d] Trade history lookup failed: %s", market_id, e)
        return None
