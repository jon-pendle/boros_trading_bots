"""
Shared test fixtures: mock API responses, mock context.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from strategies.framework.interfaces import IContext, IDataProvider, IExecutor, IStateManager
from strategies.framework.state_manager import InMemoryStateManager


# ---------------------------------------------------------------------------
# Realistic API response fixtures
# ---------------------------------------------------------------------------

MARKET_23 = {
    "marketId": 23,
    "imData": {
        "name": "Binance BTCUSDT 27 Mar 2026",
        "symbol": "BINANCE-BTCUSDT-27MAR2026",
        "maturity": 1774569600,
        "tickStep": 2,
    },
    "config": {},
    "metadata": {"platformName": "Binance", "assetSymbol": "BTC"},
    "data": {
        "markApr": 0.0206,
        "bestBid": 0.021,
        "bestAsk": 0.023,
        "assetMarkPrice": 68000.0,
        "floatingApr": 0.005,
    },
}

MARKET_24 = {
    "marketId": 24,
    "imData": {
        "name": "Binance ETHUSDT 27 Mar 2026",
        "symbol": "BINANCE-ETHUSDT-27MAR2026",
        "maturity": 1774569600,
        "tickStep": 2,
    },
    "config": {},
    "metadata": {"platformName": "Binance", "assetSymbol": "ETH"},
    "data": {
        "markApr": 0.005,
        "bestBid": 0.004,
        "bestAsk": 0.006,
        "assetMarkPrice": 2000.0,
        "floatingApr": -0.07,
    },
}

ORDERBOOK_23_RAW = {
    "long": {
        "ia": [21, 20, 19, 18],
        "sz": [
            "500000000000000000",
            "1000000000000000000",
            "2000000000000000000",
            "3000000000000000000",
        ],
    },
    "short": {
        "ia": [23, 24, 25, 26],
        "sz": [
            "800000000000000000",
            "1500000000000000000",
            "2500000000000000000",
            "4000000000000000000",
        ],
    },
}

ORDERBOOK_24_RAW = {
    "long": {
        "ia": [4, 3, 2, 1],
        "sz": [
            "10000000000000000000",
            "20000000000000000000",
            "50000000000000000000",
            "100000000000000000000",
        ],
    },
    "short": {
        "ia": [6, 7, 8, 9],
        "sz": [
            "5000000000000000000",
            "10000000000000000000",
            "15000000000000000000",
            "20000000000000000000",
        ],
    },
}

INDICATORS_RESPONSE = {
    "metadata": {"requested": ["u"], "available": ["u"]},
    "results": [
        {"ts": 1771052400, "u": 0.01},
        {"ts": 1771056000, "u": 0.005},
    ],
}

INDICATORS_NEGATIVE = {
    "metadata": {"requested": ["u"], "available": ["u"]},
    "results": [
        {"ts": 1771052400, "u": -0.03},
        {"ts": 1771056000, "u": -0.08},
    ],
}


# ---------------------------------------------------------------------------
# Mock Context
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_context():
    """Returns a mock IContext with realistic data for testing strategies."""
    ctx = MagicMock(spec=IContext)
    ctx.now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ctx.state = InMemoryStateManager()
    ctx.executor = MagicMock(spec=IExecutor)
    ctx.executor.submit_order.return_value = True
    ctx.executor.close_position.return_value = {"status": "dry_run"}
    ctx.data = MagicMock(spec=IDataProvider)
    return ctx


def setup_market_data(ctx, market_id, market_info, orderbook_raw, funding_rate,
                      tick_size=0.001):
    """Helper to configure mock data provider for a specific market."""
    from strategies.framework.data_provider import BorosDataProvider

    ctx.data.get_market_info.side_effect = lambda mid: {market_id: market_info}.get(mid, {})
    ctx.data.get_spot_price.side_effect = lambda mid: float(
        {market_id: market_info}.get(mid, {}).get('data', {}).get('assetMarkPrice', 1.0)
    )
    ctx.data.get_oracle_funding_rate.side_effect = lambda mid: {market_id: funding_rate}.get(mid, 0.0)
    ctx.data.get_all_market_ids.return_value = [market_id]
    ctx.data.get_market_name.side_effect = lambda mid: {market_id: market_info}.get(mid, {}).get('imData', {}).get('name', str(mid))

    # Parse orderbook the same way the real provider does
    bids = BorosDataProvider._parse_ob_side(orderbook_raw.get('long', {}), tick_size)
    asks = BorosDataProvider._parse_ob_side(orderbook_raw.get('short', {}), tick_size)
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    ctx.data.get_orderbook.side_effect = lambda mid, **kw: {'bids': bids, 'asks': asks}
