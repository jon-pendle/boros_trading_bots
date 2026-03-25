"""
Manually close a specific pair on Boros.

Usage:
  # Close ETH_75_76 on prod
  python close_pair.py --pair ETH_75_76 --env prod

  # Dry-run (show what would close, don't execute)
  python close_pair.py --pair ETH_75_76 --env prod --dry-run

  # Close with custom token amount (partial close)
  python close_pair.py --pair ETH_75_76 --env prod --tokens 10.0

Reads .env.prod or .env.test for credentials.
"""
import argparse
import json
import logging
import os
import sys
import time

from strategies.framework.secrets import load_secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Manually close a pair position")
    parser.add_argument("--pair", required=True, help="Pair name (e.g. ETH_75_76)")
    parser.add_argument("--env", required=True, choices=["prod", "test"], help="Environment")
    parser.add_argument("--dry-run", action="store_true", help="Show what would close without executing")
    parser.add_argument("--tokens", type=float, default=None, help="Override close amount (default: full close)")
    args = parser.parse_args()

    # Load env
    env_file = f".env.{args.env}"
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    load_secrets()

    from strategies.framework.context import ProdContext
    from strategies.framework.keystore import load_agent_key
    from strategies.framework.data_provider import BorosDataProvider
    import strategies.fr_arb.config as fr_config

    user_address = os.environ.get("USER_ADDRESS", "")
    if not user_address:
        logger.error("USER_ADDRESS not set in %s", env_file)
        sys.exit(1)

    api_url = fr_config.API_BASE_URL
    agent_key = load_agent_key()

    context = ProdContext(
        api_base_url=api_url,
        user_address=user_address,
        agent_private_key=agent_key,
    )

    # Load pair positions
    positions_file = "logs/zscore_positions.json"
    # Try from docker volume path first, then local
    for path in [positions_file, f"/app/{positions_file}"]:
        if os.path.exists(path):
            positions_file = path
            break

    if not os.path.exists(positions_file):
        logger.error("Positions file not found: %s", positions_file)
        sys.exit(1)

    with open(positions_file) as f:
        all_positions = json.load(f)

    pair_pos = all_positions.get(args.pair)
    if not pair_pos:
        logger.error("Pair %s not found in positions. Available: %s",
                     args.pair, ", ".join(sorted(all_positions.keys())))
        sys.exit(1)

    mkt_A = pair_pos["mkt_A"]
    mkt_B = pair_pos["mkt_B"]
    side_A = pair_pos["side_A"]
    side_B = pair_pos["side_B"]
    tokens = pair_pos["tokens"]
    close_tokens = args.tokens if args.tokens else tokens

    side_A_str = "SHORT" if side_A == 1 else "LONG"
    side_B_str = "SHORT" if side_B == 1 else "LONG"

    # Get current market info
    try:
        ob_A = context.data.get_orderbook(mkt_A)
        ob_B = context.data.get_orderbook(mkt_B)
        bids_A = ob_A.get("bids", [])
        asks_A = ob_A.get("asks", [])
        bids_B = ob_B.get("bids", [])
        asks_B = ob_B.get("asks", [])

        mid_A = (bids_A[0][0] + asks_A[0][0]) / 2 if bids_A and asks_A else 0
        mid_B = (bids_B[0][0] + asks_B[0][0]) / 2 if bids_B and asks_B else 0
        spread = mid_A - mid_B if side_A == 1 else mid_B - mid_A
    except Exception as e:
        logger.warning("Could not fetch OB: %s", e)
        mid_A = mid_B = spread = 0

    print(f"\n{'='*60}")
    print(f"CLOSE PAIR: {args.pair}")
    print(f"{'='*60}")
    print(f"  Env:       {args.env}")
    print(f"  Market A:  [{mkt_A}] {side_A_str}")
    print(f"  Market B:  [{mkt_B}] {side_B_str}")
    print(f"  Tokens:    {tokens:.4f} (closing {close_tokens:.4f})")
    print(f"  Mid A:     {mid_A:.4f}")
    print(f"  Mid B:     {mid_B:.4f}")
    print(f"  Spread:    {spread:.2%}")
    print(f"  Entry:     {pair_pos.get('entry_time', '?')[:19]}")
    print(f"{'='*60}")

    if args.dry_run:
        print("\n  [DRY-RUN] Would close above position. Use without --dry-run to execute.")
        return

    confirm = input(f"\nClose {close_tokens:.4f} tokens? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    # Execute close
    print("\nExecuting close...")
    result = context.executor.close_dual_position(
        mkt_a=mkt_A, side_a=side_A, tokens_a=close_tokens,
        mkt_b=mkt_B, side_b=side_B, tokens_b=close_tokens,
        tokens_wei_a="", tokens_wei_b="",
        round_id=pair_pos.get("round_id", ""),
    )

    if result:
        print(f"\nSUCCESS — closed {close_tokens:.4f} tokens")

        # Update positions file
        if close_tokens >= tokens - 0.1:
            del all_positions[args.pair]
            print(f"Removed {args.pair} from positions file")
        else:
            frac = close_tokens / tokens
            pair_pos["tokens"] -= close_tokens
            pair_pos["position_size_A"] = pair_pos.get("position_size_A", 0) * (1 - frac)
            pair_pos["position_size_B"] = pair_pos.get("position_size_B", 0) * (1 - frac)
            for layer in pair_pos.get("layers", []):
                layer["tokens"] *= (1 - frac)
            print(f"Updated {args.pair}: {pair_pos['tokens']:.4f} tokens remaining")

        with open(positions_file, "w") as f:
            json.dump(all_positions, f, indent=2, default=str)
        print(f"Saved {positions_file}")
    else:
        print("\nFAILED — check logs for details")
        sys.exit(1)


if __name__ == "__main__":
    main()
