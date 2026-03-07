"""
Boros Trade Bot - Entry Point

Usage:
    python main.py                    # dry-run with defaults
    python main.py --live             # live execution (CAUTION)
    python main.py --api-url URL      # custom API endpoint
"""
import argparse
import logging
import sys
from strategies.framework.context import LiveContext
from strategies.framework.runner import StrategyRunner
from strategies.v_shape import VShapeStrategy
import strategies.v_shape.config as v_shape_config


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

    parser = argparse.ArgumentParser(description="Boros Trade Bot")
    parser.add_argument("--api-url", default=v_shape_config.API_BASE_URL, help="Boros API base URL")
    parser.add_argument("--live", action="store_true", help="Enable live execution (default: dry-run)")
    parser.add_argument("--interval", type=int, default=v_shape_config.TICK_INTERVAL_SECONDS,
                        help="Tick interval in seconds")
    parser.add_argument("--state-file", default=v_shape_config.STATE_FILE, help="State persistence file")
    args = parser.parse_args()

    dry_run = not args.live
    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("=== Boros Trade Bot [%s] ===", mode)
    logger.info("API: %s", args.api_url)
    logger.info("Interval: %ds", args.interval)

    context = LiveContext(
        api_base_url=args.api_url,
        dry_run=dry_run,
        state_file=args.state_file,
    )

    runner = StrategyRunner(context, interval_seconds=args.interval)
    runner.add_strategy(VShapeStrategy())
    runner.run()


if __name__ == "__main__":
    main()
