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
from strategies.zscore import ZScoreStrategy
import strategies.fr_arb.config as fr_arb_config
import strategies.zscore.config as zscore_config

# Strategy registry: name -> (class, config_module)
STRATEGIES = {
    "fr_arb": (FRArbitrageStrategy, fr_arb_config),
    "zscore": (ZScoreStrategy, zscore_config),
}


AGENT_EXPIRY_WARN_DAYS = 7  # warn if expiry within this many days


def _check_agent_expiry(agent_address: str, user_address: str):
    """Check agent approval expiry via Boros API. Warn or exit if expired/expiring."""
    import time
    import requests
    logger = logging.getLogger(__name__)
    try:
        resp = requests.get(
            "https://api.boros.finance/open-api/v1/agents/expiry-time",
            params={
                "userAddress": user_address,
                "agentAddress": agent_address,
                "accountId": 0,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Agent expiry check failed: HTTP %d", resp.status_code)
            return

        expiry_ts = resp.json().get("expiryTime", 0)
        now = int(time.time())
        remaining_h = (expiry_ts - now) / 3600

        if expiry_ts == 0:
            logger.error("Agent NOT approved (expiryTime=0). Run approve_agent.py first.")
            sys.exit(1)
        elif expiry_ts <= now:
            logger.error("Agent EXPIRED %.0fh ago (expiry=%d). Run approve_agent.py to renew.",
                         -remaining_h, expiry_ts)
            sys.exit(1)
        elif remaining_h < AGENT_EXPIRY_WARN_DAYS * 24:
            logger.warning("Agent expires in %.1fh (%.1f days). Renew soon!",
                           remaining_h, remaining_h / 24)
        else:
            logger.info("Agent approved, expires in %.0f days", remaining_h / 24)
    except Exception as e:
        logger.error("Agent expiry check failed: %s", e)
        sys.exit(1)


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
    parser.add_argument("--strategy", default=os.environ.get("STRATEGY", "fr_arb"),
                        choices=list(STRATEGIES.keys()),
                        help="Strategy to run (default: fr_arb, env: STRATEGY)")
    args = parser.parse_args()

    dry_run = not args.live
    mode = "DRY-RUN" if dry_run else "LIVE"
    bot_env = os.environ.get("BOT_ENV", "dev")
    strat_name = args.strategy

    StrategyClass, strat_config = STRATEGIES[strat_name]

    sep = "=" * 60
    logger.info(sep)
    logger.info("Boros Trade Bot [%s] env=%s", mode, bot_env)
    logger.info(sep)
    logger.info("Strategy:        %s", strat_name)
    logger.info("API:             %s", args.api_url)
    logger.info("Interval:        %ds", args.interval)
    logger.info("User:            %s", fr_arb_config.USER_ADDRESS or "(none)")
    logger.info("--- Strategy Params ---")

    if strat_name == "fr_arb":
        logger.info("Entry Spread:    %.2f%%", fr_arb_config.ENTRY_SPREAD_THRESHOLD * 100)
        logger.info("Exit Spread:     %.2f%%", fr_arb_config.EXIT_SPREAD_THRESHOLD * 100)
        logger.info("Max Capital:     $%.0f (global)", fr_arb_config.MAX_CAPITAL)
    elif strat_name == "zscore":
        logger.info("Lookback:        %d ticks (~%.1f days)", zscore_config.LOOKBACK, zscore_config.LOOKBACK / 96)
        logger.info("K Entry:         %.1f sigma", zscore_config.K_ENTRY)
        logger.info("K Exit:          %.1f sigma", zscore_config.K_EXIT)
        logger.info("Use MAD:         %s", zscore_config.USE_MAD)
        logger.info("Max Capital:     $%.0f (global)", zscore_config.MAX_CAPITAL)

    logger.info("Min Hold:        %.1fh", strat_config.MIN_HOLD_HOURS)
    logger.info("Capacity Step:   %.1f tokens", strat_config.CAPACITY_STEP_TOKENS)
    logger.info("Liquidity Factor:%.2f", strat_config.LIQUIDITY_FACTOR)
    logger.info("Allowed Tokens:  %s", strat_config.ALLOWED_TOKEN_IDS or "all")
    logger.info("--- Alerts ---")
    logger.info("IFTTT:           %s", "enabled" if fr_arb_config.IFTTT_WEBHOOK_KEY else "disabled")
    logger.info("State File:      %s", args.state_file if dry_run else "API (entry_times.json)")
    logger.info(sep)

    strategy = StrategyClass()

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

        # Check agent approval expiry
        _check_agent_expiry(context.executor.signer.agent_address, user_address)
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
