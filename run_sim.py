"""
Continuous Sim Trading

Runs FR Arbitrage strategy in dry-run mode against live Boros API data.
Logs all events to JSONL file in logs/ directory for post-analysis.

Usage:
    python run_sim.py                     # default 60s interval
    python run_sim.py --interval 300      # 5 min interval
    python run_sim.py --log-dir /tmp/logs # custom log dir

Log analysis examples:
    # All entries
    cat logs/sim_*.jsonl | jq 'select(.type=="entry")'

    # Spread history for a pair
    cat logs/sim_*.jsonl | jq 'select(.type=="scan" and .pair=="BTC_20260327_60_61") | {ts, bid_a, ask_b}'

    # Position summary
    cat logs/sim_*.jsonl | jq 'select(.type=="hold" or .type=="entry" or .type=="exit")'
"""
import argparse
import logging
import sys

sys.path.insert(0, '/root/boros_trade_bot')

# Load .env BEFORE importing config modules (they read os.environ at import time)
from strategies.framework.secrets import load_secrets
load_secrets()

from strategies.framework.alert import AlertHandler, IFTTTAlert
from strategies.framework.context import LiveContext
from strategies.framework.runner import StrategyRunner
from strategies.fr_arb import FRArbitrageStrategy
import strategies.fr_arb.config as config


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="Boros FR Arb Sim Trading (continuous)")
    parser.add_argument("--interval", type=int, default=config.TICK_INTERVAL_SECONDS,
                        help="Tick interval in seconds (default: 60)")
    parser.add_argument("--log-dir", default="logs", help="Log output directory")
    args = parser.parse_args()

    logger.info("=== Boros FR Arb Sim [DRY-RUN] ===")
    logger.info("API:      %s", config.API_BASE_URL)
    logger.info("Interval: %ds", args.interval)
    logger.info("Entry:    spread > %.1f%%", config.ENTRY_SPREAD_THRESHOLD * 100)
    logger.info("Exit:     spread < %.1f%% AND hold >= %.0fh",
                config.EXIT_SPREAD_THRESHOLD * 100, config.MIN_HOLD_HOURS)
    logger.info("Capital:  $%dk (dynamic pairs)", config.MAX_CAPITAL / 1000)
    logger.info("Logs:     %s/", args.log_dir)
    logger.info("Press Ctrl+C to stop.")

    context = LiveContext(
        api_base_url=config.API_BASE_URL,
        dry_run=True,
        state_file="sim_state.json",
    )

    ifttt = IFTTTAlert(config.IFTTT_WEBHOOK_KEY, config.IFTTT_EVENT_NAME)
    alert_handler = AlertHandler(ifttt)

    runner = StrategyRunner(context, interval_seconds=args.interval,
                            log_dir=args.log_dir, alert_handler=alert_handler,
                            user_address=config.USER_ADDRESS or None)
    runner.add_strategy(FRArbitrageStrategy())
    runner.run()


if __name__ == "__main__":
    main()
