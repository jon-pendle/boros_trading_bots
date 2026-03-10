"""
Boros Trade Bot - Entry Point

Usage:
    python main.py                    # dry-run with defaults
    python main.py --live             # live execution (CAUTION)
    python main.py --api-url URL      # custom API endpoint
"""
import argparse
import logging
import os
import sys

# Load .env BEFORE importing config modules (they read os.environ at import time)
from strategies.framework.secrets import load_secrets
load_secrets()

from strategies.framework.alert import AlertHandler, IFTTTAlert
from strategies.framework.context import LiveContext, ProdContext
from strategies.framework.runner import StrategyRunner
from strategies.fr_arb import FRArbitrageStrategy
import strategies.fr_arb.config as fr_arb_config


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
    parser.add_argument("--api-url", default=fr_arb_config.API_BASE_URL, help="Boros API base URL")
    parser.add_argument("--live", action="store_true", help="Enable live execution (default: dry-run)")
    parser.add_argument("--interval", type=int, default=fr_arb_config.TICK_INTERVAL_SECONDS,
                        help="Tick interval in seconds")
    parser.add_argument("--state-file", default=fr_arb_config.STATE_FILE, help="State persistence file (sim only)")
    args = parser.parse_args()

    dry_run = not args.live
    mode = "DRY-RUN" if dry_run else "LIVE"
    bot_env = os.environ.get("BOT_ENV", "dev")

    sep = "=" * 60
    logger.info(sep)
    logger.info("Boros Trade Bot [%s] env=%s", mode, bot_env)
    logger.info(sep)
    logger.info("Strategy:        FR Arbitrage")
    logger.info("API:             %s", args.api_url)
    logger.info("Interval:        %ds", args.interval)
    logger.info("User:            %s", fr_arb_config.USER_ADDRESS or "(none)")
    logger.info("--- Strategy Params ---")
    logger.info("Entry Spread:    %.2f%%", fr_arb_config.ENTRY_SPREAD_THRESHOLD * 100)
    logger.info("Exit Spread:     %.2f%%", fr_arb_config.EXIT_SPREAD_THRESHOLD * 100)
    logger.info("Min Hold:        %.1fh", fr_arb_config.MIN_HOLD_HOURS)
    logger.info("Max Hold:        %s", "inf" if fr_arb_config.MAX_HOLD_HOURS == float('inf') else f"{fr_arb_config.MAX_HOLD_HOURS:.1f}h")
    logger.info("Max Capital:     $%.0f per pair", fr_arb_config.MAX_CAPITAL)
    logger.info("Min Entry USD:   $%.0f", fr_arb_config.MIN_ENTRY_USD)
    logger.info("Capacity Step:   %.1f tokens", fr_arb_config.CAPACITY_STEP_TOKENS)
    logger.info("Liquidity Factor:%.2f", fr_arb_config.LIQUIDITY_FACTOR)
    logger.info("Allowed Tokens:  %s", fr_arb_config.ALLOWED_TOKEN_IDS or "all")
    logger.info("--- Alerts ---")
    logger.info("IFTTT:           %s", "enabled" if fr_arb_config.IFTTT_WEBHOOK_KEY else "disabled")
    logger.info("State File:      %s", args.state_file if dry_run else "API (entry_times.json)")
    logger.info(sep)

    strategy = FRArbitrageStrategy()

    user_address = None
    if args.live:
        from strategies.framework.keystore import load_agent_key
        user_address = os.environ.get("USER_ADDRESS", "")
        if not user_address:
            logger.error("USER_ADDRESS env var required for live mode")
            sys.exit(1)
        agent_key = load_agent_key()
        context = ProdContext(
            api_base_url=args.api_url,
            user_address=user_address,
            agent_private_key=agent_key,
        )
    else:
        context = LiveContext(
            api_base_url=args.api_url,
            dry_run=True,
            state_file=args.state_file,
        )

    ifttt = IFTTTAlert(fr_arb_config.IFTTT_WEBHOOK_KEY, fr_arb_config.IFTTT_EVENT_NAME)
    alert_handler = AlertHandler(ifttt)

    runner = StrategyRunner(
        context, interval_seconds=args.interval,
        alert_handler=alert_handler, user_address=user_address,
    )
    runner.add_strategy(strategy)
    runner.run()


if __name__ == "__main__":
    main()
