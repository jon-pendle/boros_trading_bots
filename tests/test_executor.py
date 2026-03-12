"""Tests for BorosExecutor — verify on-chain execution result checking."""
import pytest
from unittest.mock import MagicMock, patch
from strategies.framework.executor import BorosExecutor


class TestSignAndSubmitVerification:
    """Test that _sign_and_submit correctly validates API response status."""

    def _make_executor(self):
        signer = MagicMock()
        signer.sign_calldatas.return_value = [{
            "agent": "0xagent",
            "message": {"account": "0xacc", "connectionId": "0xconn", "nonce": "1000"},
            "signature": "0xsig",
            "calldata": "0xcalldata",
        }]
        executor = BorosExecutor(dry_run=False, signer=signer)
        return executor

    @patch("strategies.framework.executor.requests.request")
    def test_success_status_accepted(self, mock_request):
        """All entries have status='success' → return data."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"status": "success", "txHash": "0xabc", "index": 0},
                {"status": "success", "txHash": "0xdef", "index": 1},
            ],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0xcalldata1", "0xcalldata2"])
        assert result is not None
        assert len(result) == 2

    @patch("strategies.framework.executor.requests.request")
    def test_reverted_status_rejected(self, mock_request):
        """Any entry with status='reverted' → return None."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"status": "success", "txHash": "0xabc", "index": 0},
                {"status": "reverted", "error": "AuthInvalidMessage()", "index": 1},
            ],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0xcalldata1", "0xcalldata2"])
        assert result is None

    @patch("strategies.framework.executor.requests.request")
    def test_pending_status_rejected(self, mock_request):
        """Any entry with status != 'success' (e.g. 'pending') → return None."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"status": "pending", "txHash": "0xabc", "index": 0},
            ],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0xcalldata"])
        assert result is None

    @patch("strategies.framework.executor.requests.request")
    def test_unknown_status_rejected(self, mock_request):
        """Entry with empty/missing status → return None."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"txHash": "0xabc", "index": 0}],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0xcalldata"])
        assert result is None

    @patch("strategies.framework.executor.requests.request")
    def test_http_error_rejected(self, mock_request):
        """HTTP 400 → return None."""
        mock_request.return_value = MagicMock(
            status_code=400, text="Bad Request",
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0xcalldata"])
        assert result is None

    @patch("strategies.framework.executor.requests.request")
    def test_http_201_with_success(self, mock_request):
        """HTTP 201 with status='success' → accepted."""
        mock_request.return_value = MagicMock(
            status_code=201,
            json=lambda: [{"status": "success", "txHash": "0xabc", "index": 0}],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0xcalldata"])
        assert result is not None

    @patch("strategies.framework.executor.requests.request")
    def test_mm_market_not_entered_ignored(self, mock_request):
        """enterMarket revert with MMMarketNotEntered is benign → accepted.
        Reproduces real scenario: dual close sends 4 calldatas
        (enterMarket + close for each leg). If market already entered,
        enterMarket simulate reverts but close succeeds."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"status": "success", "txHash": "0xabc", "index": 0},  # enterMarket(A)
                {"status": "success", "txHash": "0xabc", "index": 1},  # close(A)
                {"status": "reverted", "error": "[SIMULATE] MMMarketNotEntered()", "index": 2},  # enterMarket(B) - already entered
                {"status": "success", "txHash": "0xabc", "index": 3},  # close(B)
            ],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0x1", "0x2", "0x3", "0x4"])
        assert result is not None
        assert len(result) == 4

    @patch("strategies.framework.executor.requests.request")
    def test_market_already_entered_ignored(self, mock_request):
        """MarketAlreadyEntered revert is also benign → accepted."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"status": "reverted", "error": "MarketAlreadyEntered()", "index": 0},
                {"status": "success", "txHash": "0xdef", "index": 1},
            ],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0x1", "0x2"])
        assert result is not None

    @patch("strategies.framework.executor.requests.request")
    def test_real_revert_still_rejected(self, mock_request):
        """Non-benign revert (e.g. AuthInvalidMessage) still fails."""
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"status": "success", "txHash": "0xabc", "index": 0},
                {"status": "reverted", "error": "AuthInvalidMessage()", "index": 1},
            ],
        )
        executor = self._make_executor()
        result = executor._sign_and_submit(["0x1", "0x2"])
        assert result is None
