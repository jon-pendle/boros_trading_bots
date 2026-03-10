from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime


class IDataProvider(ABC):
    """Abstract interface for all market data access (API-driven)."""

    @abstractmethod
    def get_oracle_funding_rate(self, market_id: int) -> Optional[float]:
        """Returns the Oracle Funding Rate (Settlement APR 'u') from Indicators API.
        Returns None if API call fails (distinguishes from real 0.0)."""
        pass

    @abstractmethod
    def get_spot_price(self, market_id: int) -> Optional[float]:
        """Returns the spot price of the underlying asset. None if unavailable."""
        pass

    @abstractmethod
    def get_market_info(self, market_id: int) -> Dict[str, Any]:
        """Returns market info (tickStep, maturity, minSize, etc)."""
        pass

    @abstractmethod
    def get_orderbook(self, market_id: int) -> dict[str, list[tuple[float, float]]]:
        """
        Returns Orderbook from API.
        Format: {'bids': [(price, size), ...], 'asks': [(price, size), ...]}
        Bids sorted Descending, Asks sorted Ascending.
        """
        pass

    @abstractmethod
    def get_all_market_ids(self) -> list[int]:
        """Returns all available market IDs from the API."""
        pass


class IExecutor(ABC):
    """Abstract interface for order execution."""

    @abstractmethod
    def submit_order(self, market_id: int, side: int, size_tokens: float,
                     limit_tick: Optional[float] = None, round_id: Optional[str] = None) -> bool:
        """
        Submits an order.
        side: 0=LONG, 1=SHORT
        limit_tick: Price expressed in Ticks.
        """
        pass

    @abstractmethod
    def close_position(self, market_id: int, side: int, size_usd: float = 0.0,
                       tokens: float = 0.0, round_id: Optional[str] = None) -> Optional[dict]:
        """Closes an active position. Returns trade details or None on failure."""
        pass

    def submit_dual_order(self, mkt_a: int, side_a: int, mkt_b: int, side_b: int,
                          size_tokens: float, limit_tick_a: Optional[float] = None,
                          limit_tick_b: Optional[float] = None,
                          round_id: Optional[str] = None) -> bool:
        """Atomic dual-market order. Default: submit two separate orders."""
        ok_a = self.submit_order(mkt_a, side_a, size_tokens, limit_tick_a, round_id)
        ok_b = self.submit_order(mkt_b, side_b, size_tokens, limit_tick_b, round_id)
        return ok_a and ok_b

    def close_dual_position(self, mkt_a: int, side_a: int, tokens_a: float,
                            mkt_b: int, side_b: int, tokens_b: float,
                            round_id: Optional[str] = None) -> Optional[dict]:
        """Atomic dual close. Default: close two separate positions."""
        r_a = self.close_position(mkt_a, side_a, tokens=tokens_a, round_id=round_id)
        r_b = self.close_position(mkt_b, side_b, tokens=tokens_b, round_id=round_id)
        if r_a and r_b:
            return {"market_a": mkt_a, "market_b": mkt_b, "status": "ok"}
        return None


class IStateManager(ABC):
    """Abstract interface for position state persistence."""

    @abstractmethod
    def get_position(self, strategy_name: str, market_id: int) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def get_all_positions(self, strategy_name: str) -> Dict[int, Dict[str, Any]]:
        """Returns all open positions for a strategy. {market_id: position_data}"""
        pass

    @abstractmethod
    def set_position(self, strategy_name: str, market_id: int, data: Dict[str, Any]):
        pass

    @abstractmethod
    def clear_position(self, strategy_name: str, market_id: int):
        pass


class IContext(ABC):
    """Aggregate context injected into strategies each tick."""
    data: IDataProvider
    executor: IExecutor
    state: IStateManager
    now: datetime
