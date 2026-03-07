"""
Test all Boros API endpoints via BorosDataProvider.
Validates connectivity, response parsing, and data quality.
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/root/boros_trade_bot')
from strategies.framework.data_provider import BorosDataProvider

API_URL = "https://api.boros.finance/core"


def test_get_all_markets(provider: BorosDataProvider):
    print("\n=== Test: get_all_market_ids ===")
    ids = provider.get_all_market_ids()
    print(f"  Found {len(ids)} markets: {ids[:10]}...")
    assert len(ids) > 0, "No markets returned"
    return ids


def test_get_market_info(provider: BorosDataProvider, market_id: int):
    print(f"\n=== Test: get_market_info ({market_id}) ===")
    info = provider.get_market_info(market_id)
    im = info.get('imData', {})
    meta = info.get('metadata', {})
    d = info.get('data', {})
    print(f"  Name:       {im.get('name')}")
    print(f"  Symbol:     {im.get('symbol')}")
    print(f"  Maturity:   {im.get('maturity')}")
    print(f"  TickStep:   {im.get('tickStep')}")
    print(f"  Platform:   {meta.get('platformName')}")
    print(f"  BestBid:    {d.get('bestBid')}")
    print(f"  BestAsk:    {d.get('bestAsk')}")
    print(f"  MarkAPR:    {d.get('markApr')}")
    assert im.get('name'), "No imData.name"
    return info


def test_get_oracle_funding_rate(provider: BorosDataProvider, market_id: int):
    print(f"\n=== Test: get_oracle_funding_rate ({market_id}) ===")
    rate = provider.get_oracle_funding_rate(market_id)
    print(f"  Oracle Funding (u): {rate:.6f} ({rate:.2%})")
    return rate


def test_get_mark_apr(provider: BorosDataProvider, market_id: int):
    print(f"\n=== Test: get_mark_apr ({market_id}) ===")
    apr = provider.get_mark_apr(market_id)
    print(f"  Mark APR (ap): {apr:.6f} ({apr:.2%})")
    return apr


def test_get_spot_price(provider: BorosDataProvider, market_id: int):
    print(f"\n=== Test: get_spot_price ({market_id}) ===")
    price = provider.get_spot_price(market_id)
    print(f"  Spot Price: ${price}")
    return price


def test_get_orderbook(provider: BorosDataProvider, market_id: int):
    print(f"\n=== Test: get_orderbook ({market_id}) ===")
    book = provider.get_orderbook(market_id)
    bids = book.get('bids', [])
    asks = book.get('asks', [])
    print(f"  Bid levels: {len(bids)}, Ask levels: {len(asks)}")
    if bids:
        print(f"  Best Bid: rate={bids[0][0]:.6f}, size={bids[0][1]:.4f} tokens")
        total_bid = sum(s for _, s in bids)
        print(f"  Total Bid Depth: {total_bid:.2f} tokens")
    if asks:
        print(f"  Best Ask: rate={asks[0][0]:.6f}, size={asks[0][1]:.4f} tokens")
        total_ask = sum(s for _, s in asks)
        print(f"  Total Ask Depth: {total_ask:.2f} tokens")
    if bids and asks:
        spread = asks[0][0] - bids[0][0]
        print(f"  Spread: {spread:.6f}")
    assert len(bids) > 0 or len(asks) > 0, "Empty orderbook"
    return book


def test_helpers(provider: BorosDataProvider, market_id: int):
    print(f"\n=== Test: helpers ({market_id}) ===")
    print(f"  get_tick_step:    {provider.get_tick_step(market_id)}")
    print(f"  get_maturity:     {provider.get_maturity(market_id)}")
    print(f"  get_market_name:  {provider.get_market_name(market_id)}")
    bb, ba = provider.get_best_bid_ask(market_id)
    print(f"  get_best_bid_ask: bid={bb}, ask={ba}")


def main():
    print(f"Boros API Test - {datetime.now(timezone.utc).isoformat()}")
    print(f"Base URL: {API_URL}")

    provider = BorosDataProvider(api_base_url=API_URL)

    # 1. List all markets
    market_ids = test_get_all_markets(provider)

    # 2. Deep test on first market
    test_id = market_ids[0]
    print(f"\n{'='*50}")
    print(f"Deep test: market_id={test_id}")
    print(f"{'='*50}")

    test_get_market_info(provider, test_id)
    test_get_oracle_funding_rate(provider, test_id)
    test_get_mark_apr(provider, test_id)
    test_get_spot_price(provider, test_id)
    test_get_orderbook(provider, test_id)
    test_helpers(provider, test_id)

    # 3. Quick scan across multiple markets
    print(f"\n{'='*50}")
    print("Quick scan across all markets")
    print(f"{'='*50}")
    for mid in market_ids:
        name = provider.get_market_name(mid)
        rate = provider.get_oracle_funding_rate(mid)
        book = provider.get_orderbook(mid)
        n_bids = len(book.get('bids', []))
        n_asks = len(book.get('asks', []))
        bb, ba = provider.get_best_bid_ask(mid)
        print(f"  [{mid:3d}] {name:40s} | u={rate:+.4f} | bid={bb:.4f} ask={ba:.4f} | OB: {n_bids}b/{n_asks}a")

    print(f"\n=== ALL {len(market_ids)} MARKETS TESTED - ALL API ENDPOINTS WORKING ===")


if __name__ == "__main__":
    main()
