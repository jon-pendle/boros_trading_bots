"""
LiveContext wires together DataProvider + Executor + StateManager for live trading.
"""
from datetime import datetime, timezone
from .interfaces import IContext, IDataProvider, IExecutor, IStateManager
from .data_provider import BorosDataProvider
from .executor import BorosExecutor
from .state_manager import JsonFileStateManager


class LiveContext(IContext):
    def __init__(self, api_base_url: str, dry_run: bool = True,
                 state_file: str = "bot_state.json"):
        self._data = BorosDataProvider(api_base_url)
        self._executor = BorosExecutor(api_base_url, dry_run=dry_run)
        self._state = JsonFileStateManager(state_file)

    @property
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def data(self) -> IDataProvider:
        return self._data

    @property
    def executor(self) -> IExecutor:
        return self._executor

    @property
    def state(self) -> IStateManager:
        return self._state
