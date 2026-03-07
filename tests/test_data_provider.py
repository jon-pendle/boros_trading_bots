"""Tests for BorosDataProvider - mock HTTP, test response parsing, retry logic."""
import pytest
import requests
from unittest.mock import patch, MagicMock
from strategies.framework.data_provider import BorosDataProvider
from tests.conftest import (
    MARKET_23, MARKET_24, ORDERBOOK_23_RAW,
    INDICATORS_RESPONSE, INDICATORS_NEGATIVE,
)


@pytest.fixture
def provider():
    return BorosDataProvider("https://api.boros.finance/core")


class TestParseObSide:
    def test_normal(self):
        side = {"ia": [21, 20, 19], "sz": ["500000000000000000", "1000000000000000000", "2000000000000000000"]}
        result = BorosDataProvider._parse_ob_side(side, tick_size=0.001)
        assert len(result) == 3
        assert result[0] == (0.021, 0.5)
        assert result[1] == (0.020, 1.0)
        assert result[2] == (0.019, 2.0)

    def test_empty(self):
        assert BorosDataProvider._parse_ob_side({}, 0.001) == []
        assert BorosDataProvider._parse_ob_side(None, 0.001) == []

    def test_zero_size_filtered(self):
        side = {"ia": [10], "sz": ["0"]}
        assert BorosDataProvider._parse_ob_side(side, 0.001) == []

    def test_negative_tick_index(self):
        side = {"ia": [-5, -10], "sz": ["1000000000000000000", "2000000000000000000"]}
        result = BorosDataProvider._parse_ob_side(side, 0.001)
        assert result[0] == (-0.005, 1.0)
        assert result[1] == (-0.010, 2.0)

    def test_mismatched_lengths(self):
        side = {"ia": [10, 20, 30], "sz": ["1000000000000000000"]}
        result = BorosDataProvider._parse_ob_side(side, 0.001)
        assert len(result) == 1


class TestGetAllMarketIds:
    def test_parses_results(self, provider):
        with patch.object(provider, '_get', return_value={"results": [MARKET_23, MARKET_24]}):
            ids = provider.get_all_market_ids()
            assert ids == [23, 24]
            # Should populate cache
            assert 23 in provider._market_cache
            assert 24 in provider._market_cache

    def test_empty_response(self, provider):
        with patch.object(provider, '_get', return_value=None):
            assert provider.get_all_market_ids() == []

    def test_no_results_key(self, provider):
        with patch.object(provider, '_get', return_value={"data": []}):
            assert provider.get_all_market_ids() == []


class TestGetMarketInfo:
    def test_from_cache(self, provider):
        provider._market_cache[23] = MARKET_23
        info = provider.get_market_info(23)
        assert info["imData"]["name"] == "Binance BTCUSDT 27 Mar 2026"

    def test_from_api(self, provider):
        with patch.object(provider, '_get', return_value=MARKET_23):
            info = provider.get_market_info(23)
            assert info["imData"]["maturity"] == 1774569600
            assert 23 in provider._market_cache

    def test_not_found(self, provider):
        with patch.object(provider, '_get', return_value=None):
            assert provider.get_market_info(999) == {}


class TestGetOracleFundingRate:
    def test_returns_last_value(self, provider):
        with patch.object(provider, '_get', return_value=INDICATORS_RESPONSE):
            rate = provider.get_oracle_funding_rate(23)
            assert rate == 0.005

    def test_negative_rate(self, provider):
        with patch.object(provider, '_get', return_value=INDICATORS_NEGATIVE):
            rate = provider.get_oracle_funding_rate(24)
            assert rate == -0.08

    def test_api_failure_returns_none(self, provider):
        with patch.object(provider, '_get', return_value=None):
            assert provider.get_oracle_funding_rate(23) is None

    def test_empty_results(self, provider):
        with patch.object(provider, '_get', return_value={"metadata": {}, "results": []}):
            assert provider.get_oracle_funding_rate(23) == 0.0


class TestGetSpotPrice:
    def test_from_market_data(self, provider):
        provider._market_cache[23] = MARKET_23
        assert provider.get_spot_price(23) == 68000.0

    def test_fallback(self, provider):
        provider._market_cache[99] = {"data": {}}
        assert provider.get_spot_price(99) == 1.0


class TestGetOrderbook:
    def test_parses_correctly(self, provider):
        with patch.object(provider, '_get', return_value=ORDERBOOK_23_RAW):
            book = provider.get_orderbook(23)
            bids = book['bids']
            asks = book['asks']
            # long=bids (descending), short=asks (ascending)
            assert bids[0][0] == 0.021  # highest bid
            assert asks[0][0] == 0.023  # lowest ask
            assert bids[0][0] < asks[0][0]  # bid < ask (no crossing)

    def test_empty(self, provider):
        with patch.object(provider, '_get', return_value=None):
            book = provider.get_orderbook(23)
            assert book == {'bids': [], 'asks': []}


class TestHelpers:
    def test_get_tick_step(self, provider):
        provider._market_cache[23] = MARKET_23
        assert provider.get_tick_step(23) == 2.0

    def test_get_maturity(self, provider):
        provider._market_cache[23] = MARKET_23
        assert provider.get_maturity(23) == 1774569600

    def test_get_market_name(self, provider):
        provider._market_cache[23] = MARKET_23
        assert provider.get_market_name(23) == "Binance BTCUSDT 27 Mar 2026"

    def test_get_best_bid_ask(self, provider):
        provider._market_cache[23] = MARKET_23
        bb, ba = provider.get_best_bid_ask(23)
        assert bb == 0.021
        assert ba == 0.023

    def test_get_mark_apr(self, provider):
        provider._market_cache[23] = MARKET_23
        assert provider.get_mark_apr(23) == 0.0206


class TestRetryLogic:
    @patch('strategies.framework.data_provider.time.sleep')
    def test_retries_on_500(self, mock_sleep, provider):
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {"results": []}

        with patch('strategies.framework.data_provider.requests.get',
                   side_effect=[mock_resp_500, mock_resp_ok]) as mock_get:
            result = provider._get("/v1/markets")
            assert result == {"results": []}
            assert mock_get.call_count == 2
            mock_sleep.assert_called_once_with(1.0)

    @patch('strategies.framework.data_provider.time.sleep')
    def test_retries_on_timeout(self, mock_sleep, provider):
        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {"ok": True}

        with patch('strategies.framework.data_provider.requests.get',
                   side_effect=[requests.exceptions.Timeout(), mock_resp_ok]):
            result = provider._get("/v1/test")
            assert result == {"ok": True}

    @patch('strategies.framework.data_provider.time.sleep')
    def test_no_retry_on_404(self, mock_sleep, provider):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch('strategies.framework.data_provider.requests.get',
                   return_value=mock_resp) as mock_get:
            result = provider._get("/v1/notfound")
            assert result is None
            assert mock_get.call_count == 1
            mock_sleep.assert_not_called()

    @patch('strategies.framework.data_provider.time.sleep')
    def test_all_retries_exhausted(self, mock_sleep, provider):
        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with patch('strategies.framework.data_provider.requests.get',
                   return_value=mock_resp) as mock_get:
            result = provider._get("/v1/fail")
            assert result is None
            assert mock_get.call_count == 3

    def test_spot_price_returns_none_on_missing_info(self, provider):
        with patch.object(provider, '_get', return_value=None):
            assert provider.get_spot_price(999) is None
