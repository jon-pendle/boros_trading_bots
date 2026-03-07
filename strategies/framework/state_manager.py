"""
State managers for position persistence.
- InMemoryStateManager: volatile, for testing
- JsonFileStateManager: persists to disk, survives restarts
"""
import json
import os
from typing import Dict, Any, Optional
from .interfaces import IStateManager


class InMemoryStateManager(IStateManager):
    def __init__(self):
        self._positions: Dict[str, Dict[str, Any]] = {}

    def get_position(self, strategy_name: str, market_id: int) -> Optional[Dict[str, Any]]:
        return self._positions.get(f"{strategy_name}_{market_id}")

    def get_all_positions(self, strategy_name: str) -> Dict[int, Dict[str, Any]]:
        result = {}
        prefix = f"{strategy_name}_"
        for key, data in self._positions.items():
            if key.startswith(prefix):
                try:
                    market_id = int(key[len(prefix):])
                    result[market_id] = data
                except ValueError:
                    pass
        return result

    def set_position(self, strategy_name: str, market_id: int, data: Dict[str, Any]):
        self._positions[f"{strategy_name}_{market_id}"] = data

    def clear_position(self, strategy_name: str, market_id: int):
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

    def get_position(self, strategy_name: str, market_id: int) -> Optional[Dict[str, Any]]:
        return self._positions.get(f"{strategy_name}_{market_id}")

    def get_all_positions(self, strategy_name: str) -> Dict[int, Dict[str, Any]]:
        result = {}
        prefix = f"{strategy_name}_"
        for key, data in self._positions.items():
            if key.startswith(prefix):
                try:
                    market_id = int(key[len(prefix):])
                    result[market_id] = data
                except ValueError:
                    pass
        return result

    def set_position(self, strategy_name: str, market_id: int, data: Dict[str, Any]):
        self._positions[f"{strategy_name}_{market_id}"] = data
        self._save()

    def clear_position(self, strategy_name: str, market_id: int):
        key = f"{strategy_name}_{market_id}"
        if self._positions.pop(key, None) is not None:
            self._save()
