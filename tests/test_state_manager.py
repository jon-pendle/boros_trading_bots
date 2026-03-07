"""Tests for InMemoryStateManager and JsonFileStateManager."""
import os
import json
import tempfile
import pytest
from strategies.framework.state_manager import InMemoryStateManager, JsonFileStateManager


class TestInMemoryStateManager:
    def test_set_and_get(self):
        sm = InMemoryStateManager()
        sm.set_position("strat", 10, {"side": 1, "size": 1000})
        pos = sm.get_position("strat", 10)
        assert pos == {"side": 1, "size": 1000}

    def test_get_nonexistent(self):
        sm = InMemoryStateManager()
        assert sm.get_position("strat", 99) is None

    def test_clear_position(self):
        sm = InMemoryStateManager()
        sm.set_position("strat", 10, {"side": 1})
        sm.clear_position("strat", 10)
        assert sm.get_position("strat", 10) is None

    def test_clear_nonexistent_no_error(self):
        sm = InMemoryStateManager()
        sm.clear_position("strat", 99)  # should not raise

    def test_get_all_positions(self):
        sm = InMemoryStateManager()
        sm.set_position("strat", 10, {"a": 1})
        sm.set_position("strat", 20, {"a": 2})
        sm.set_position("other", 10, {"a": 3})
        result = sm.get_all_positions("strat")
        assert result == {10: {"a": 1}, 20: {"a": 2}}

    def test_get_all_empty(self):
        sm = InMemoryStateManager()
        assert sm.get_all_positions("strat") == {}

    def test_strategy_isolation(self):
        sm = InMemoryStateManager()
        sm.set_position("alpha", 10, {"v": 1})
        sm.set_position("beta", 10, {"v": 2})
        assert sm.get_position("alpha", 10)["v"] == 1
        assert sm.get_position("beta", 10)["v"] == 2

    def test_overwrite_position(self):
        sm = InMemoryStateManager()
        sm.set_position("strat", 10, {"v": 1})
        sm.set_position("strat", 10, {"v": 2})
        assert sm.get_position("strat", 10)["v"] == 2


class TestJsonFileStateManager:
    def test_persistence(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sm1 = JsonFileStateManager(path)
            sm1.set_position("strat", 10, {"side": 1, "size": 500})

            # New instance reads from same file
            sm2 = JsonFileStateManager(path)
            pos = sm2.get_position("strat", 10)
            assert pos == {"side": 1, "size": 500}
        finally:
            os.unlink(path)

    def test_clear_persists(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sm = JsonFileStateManager(path)
            sm.set_position("strat", 10, {"v": 1})
            sm.clear_position("strat", 10)

            sm2 = JsonFileStateManager(path)
            assert sm2.get_position("strat", 10) is None
        finally:
            os.unlink(path)

    def test_corrupted_file_handled(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode='w') as f:
            f.write("not valid json{{{")
            path = f.name
        try:
            sm = JsonFileStateManager(path)
            assert sm.get_all_positions("strat") == {}
        finally:
            os.unlink(path)

    def test_missing_file_handled(self):
        sm = JsonFileStateManager("/tmp/nonexistent_test_state_12345.json")
        assert sm.get_all_positions("strat") == {}
