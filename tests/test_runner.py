"""Tests for StrategyRunner circuit breaker and tick summary."""
import pytest
import time
from unittest.mock import MagicMock, patch
from strategies.framework.runner import CircuitBreaker


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        assert cb.is_open is False

    def test_stays_closed_under_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True

    def test_success_resets_count(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.is_open is False
        assert cb.consecutive_failures == 1

    def test_closes_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True
        # Simulate cooldown expiry
        cb.open_until = time.time() - 1
        assert cb.is_open is False
        assert cb.consecutive_failures == 0

    def test_reopens_on_new_failures(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True
        # Expire cooldown
        cb.open_until = time.time() - 1
        assert cb.is_open is False
        # New failures trip it again
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True
