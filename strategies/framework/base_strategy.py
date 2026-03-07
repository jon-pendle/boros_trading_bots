from abc import ABC, abstractmethod
from .interfaces import IContext


class BaseStrategy(ABC):
    def __init__(self, name: str, target_markets: list[int] | None = None):
        self.name = name
        self.target_markets = target_markets or []

    @abstractmethod
    def on_tick(self, context: IContext):
        """
        Main logic loop called each interval.
        Access data via context.data
        Execute via context.executor
        Persist via context.state
        """
        pass
