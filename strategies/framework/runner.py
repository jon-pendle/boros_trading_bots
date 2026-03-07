"""
Strategy Runner - main loop that ticks strategies at a configurable interval.
Supports running multiple strategies concurrently.
Writes structured JSONL logs for post-analysis.
Includes circuit breaker to pause on consecutive failures.
"""
import json
import logging
import time
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from .alert import AlertHandler, IFTTTAlert
from .base_strategy import BaseStrategy
from .context import LiveContext

logger = logging.getLogger(__name__)

# Circuit breaker constants
CB_FAILURE_THRESHOLD = 5      # consecutive tick failures to trip
CB_COOLDOWN_SECONDS = 300     # 5 minutes cooldown when tripped


class CircuitBreaker:
    """Simple circuit breaker: CLOSED -> OPEN after N consecutive failures."""

    def __init__(self, failure_threshold: int = CB_FAILURE_THRESHOLD,
                 cooldown_seconds: int = CB_COOLDOWN_SECONDS):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_failures = 0
        self.open_until: float = 0.0  # timestamp when CB closes again

    @property
    def is_open(self) -> bool:
        if self.open_until > 0:
            if time.time() >= self.open_until:
                # Cooldown expired, transition back to CLOSED
                logger.info("Circuit breaker cooldown expired, resuming")
                self.open_until = 0.0
                self.consecutive_failures = 0
                return False
            return True
        return False

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.open_until = time.time() + self.cooldown_seconds
            logger.error(
                "Circuit breaker OPEN after %d consecutive failures. "
                "Cooling down for %ds",
                self.consecutive_failures, self.cooldown_seconds,
            )


class StrategyRunner:
    def __init__(self, context: LiveContext, interval_seconds: int = 60,
                 log_dir: str = "logs",
                 alert_handler: AlertHandler = None):
        self.context = context
        self.interval = interval_seconds
        self.strategies: list[BaseStrategy] = []
        self._running = False
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = None
        self._tick_count = 0
        self._circuit_breaker = CircuitBreaker()
        self._heartbeat_path = self.log_dir / "heartbeat.json"
        self._alert_handler = alert_handler

    def add_strategy(self, strategy: BaseStrategy):
        self.strategies.append(strategy)
        logger.info("Registered strategy: %s", strategy.name)

    def run(self):
        """Main loop. Ticks all registered strategies at the configured interval."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Open JSONL log file (append mode, one file per session)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"sim_{session_id}.jsonl"
        self._log_file = open(log_path, 'a')

        logger.info(
            "Starting with %d strategy(ies), interval=%ds",
            len(self.strategies), self.interval,
        )
        logger.info("Logging to %s", log_path)

        try:
            while self._running:
                tick_start = time.time()
                now = datetime.now(timezone.utc)
                self._tick_count += 1

                # Circuit breaker check
                if self._circuit_breaker.is_open:
                    remaining = self._circuit_breaker.open_until - time.time()
                    logger.warning(
                        "Circuit breaker OPEN, skipping tick #%d (%.0fs remaining)",
                        self._tick_count, remaining,
                    )
                    self._write_heartbeat(now, status="circuit_breaker_open")
                    time.sleep(min(self.interval, 30))
                    continue

                logger.info("--- Tick #%d %s ---", self._tick_count, now.strftime('%H:%M:%S'))

                # Invalidate market cache each tick so we get fresh data
                self.context.data._market_cache.clear()

                tick_had_failure = False
                all_events = []

                for strategy in self.strategies:
                    try:
                        events = strategy.on_tick(self.context)
                        if events:
                            self._process_events(now, strategy.name, events)
                            all_events.extend(events)
                    except Exception as e:
                        tick_had_failure = True
                        logger.error(
                            "Error in %s: %s", strategy.name, e, exc_info=True,
                        )

                # Dispatch alerts
                if self._alert_handler and all_events:
                    try:
                        self._alert_handler.handle_events(all_events)
                    except Exception as e:
                        logger.warning("Alert handler error: %s", e)

                # Circuit breaker tracking
                if tick_had_failure:
                    self._circuit_breaker.record_failure()
                else:
                    self._circuit_breaker.record_success()

                elapsed = time.time() - tick_start

                # Write tick_summary event
                self._write_tick_summary(now, elapsed, all_events)

                # Write heartbeat
                self._write_heartbeat(now, status="ok", elapsed=elapsed)

                logger.info("Tick completed in %.1fs", elapsed)
                sleep_time = max(0, self.interval - elapsed)
                if sleep_time > 0 and self._running:
                    time.sleep(sleep_time)
        finally:
            if self._log_file:
                self._log_file.close()
            logger.info("Stopped after %d ticks. Log: %s", self._tick_count, log_path)

    def _process_events(self, tick_time: datetime, strategy_name: str, events: list[dict]):
        """Write events to JSONL and print summary to console."""
        entries = [e for e in events if e.get('type') == 'entry']
        exits = [e for e in events if e.get('type') == 'exit']
        holds = [e for e in events if e.get('type') == 'hold']
        skips = [e for e in events if e.get('type') == 'skip']
        scans = [e for e in events if e.get('type') == 'scan']

        n_signal = len(entries) + len(skips)
        logger.info(
            "[%s] Scanned %d markets | Signals: %d | Entries: %d | Exits: %d | Holding: %d",
            strategy_name, len(scans), n_signal, len(entries), len(exits), len(holds),
        )

        for e in entries:
            logger.info(
                "  ENTRY [%d] %s | u=%+.4f | $%s",
                e['market_id'], e['symbol'][:30], e['funding_rate'], e['position_size'],
            )
        for e in exits:
            logger.info(
                "  EXIT  [%d] %s | u=%+.4f | held %.1fh",
                e['market_id'], e['symbol'][:30], e['funding_rate'], e['duration_hours'],
            )
        for e in holds:
            logger.info(
                "  HOLD  [%d] %s | u=%+.4f | entry=%+.4f | %.1fh",
                e['market_id'], e['symbol'][:30], e['funding_rate'],
                e['entry_rate'], e['duration_hours'],
            )

        # Write ALL events to JSONL
        ts = tick_time.isoformat()
        for event in events:
            record = {
                "ts": ts,
                "tick": self._tick_count,
                "strategy": strategy_name,
                **event,
            }
            line = json.dumps(record, default=str)
            self._log_file.write(line + '\n')
        self._log_file.flush()

    def _write_tick_summary(self, tick_time: datetime, elapsed: float, events: list[dict]):
        """Write a tick_summary event to JSONL for monitoring."""
        summary = {
            "ts": tick_time.isoformat(),
            "tick": self._tick_count,
            "type": "tick_summary",
            "elapsed_seconds": round(elapsed, 3),
            "total_events": len(events),
            "entries": sum(1 for e in events if e.get('type') == 'entry'),
            "exits": sum(1 for e in events if e.get('type') == 'exit'),
            "holds": sum(1 for e in events if e.get('type') == 'hold'),
            "skips": sum(1 for e in events if e.get('type') == 'skip'),
            "scans": sum(1 for e in events if e.get('type') == 'scan'),
            "cb_failures": self._circuit_breaker.consecutive_failures,
        }
        self._log_file.write(json.dumps(summary) + '\n')
        self._log_file.flush()

    def _write_heartbeat(self, now: datetime, status: str = "ok", elapsed: float = 0.0):
        """Write heartbeat.json for external monitoring."""
        heartbeat = {
            "last_tick": now.isoformat(),
            "tick_count": self._tick_count,
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
            "strategies": [s.name for s in self.strategies],
        }
        try:
            self._heartbeat_path.write_text(json.dumps(heartbeat, indent=2))
        except Exception as e:
            logger.warning("Failed to write heartbeat: %s", e)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received. Stopping...")
        self._running = False
