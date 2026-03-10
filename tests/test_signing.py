"""Tests for EIP-712 agent signing module."""
import time
import pytest
from strategies.framework.signing import AgentSigner, pack_account, BOROS_ROUTER_ADDRESS

# Test private key (DO NOT use in production)
TEST_PRIVATE_KEY = "0x" + "ab" * 32
TEST_ROOT_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"


class TestPackAccount:
    def test_pack_returns_21_bytes(self):
        """SDK AccountLib.pack() produces 21 bytes: (root << 8) | accountId."""
        result = pack_account(TEST_ROOT_ADDRESS, 0)
        assert len(result) == 21

    def test_pack_matches_sdk_format(self):
        """Matches SDK: root address left-shifted 8 bits + accountId byte."""
        result = pack_account(TEST_ROOT_ADDRESS, 0)
        expected_int = (int(TEST_ROOT_ADDRESS, 16) << 8) | 0
        assert result == expected_int.to_bytes(21, byteorder='big')

    def test_pack_account_id_zero(self):
        result = pack_account(TEST_ROOT_ADDRESS, 0)
        # Last byte should be 0
        assert result[-1] == 0
        # First 20 bytes shifted: address bytes appear shifted left by 1 byte
        addr_bytes = bytes.fromhex(TEST_ROOT_ADDRESS[2:])
        assert result[:20] == addr_bytes  # address is in high 20 bytes

    def test_pack_account_id_nonzero(self):
        result = pack_account(TEST_ROOT_ADDRESS, 1)
        assert result[-1] == 1
        result_5 = pack_account(TEST_ROOT_ADDRESS, 5)
        assert result_5[-1] == 5


class TestAgentSigner:
    def setup_method(self):
        self.signer = AgentSigner(TEST_PRIVATE_KEY, TEST_ROOT_ADDRESS)

    def test_init_sets_agent_address(self):
        assert self.signer.agent_address.startswith("0x")
        assert len(self.signer.agent_address) == 42

    def test_sign_calldata_returns_required_fields(self):
        nonce = int(time.time() * 1000) * 1000
        result = self.signer.sign_calldata("0xdeadbeef", nonce=nonce)
        assert "agent" in result
        assert "message" in result
        assert "signature" in result
        assert "calldata" in result

    def test_sign_calldata_message_structure(self):
        nonce = 1000000
        result = self.signer.sign_calldata("0xdeadbeef", nonce=nonce)
        msg = result["message"]
        assert "account" in msg
        assert "connectionId" in msg
        assert "nonce" in msg
        assert msg["nonce"] == "1000000"

    def test_sign_calldata_agent_matches(self):
        result = self.signer.sign_calldata("0xdeadbeef", nonce=1)
        assert result["agent"] == self.signer.agent_address

    def test_sign_calldata_preserves_calldata(self):
        result = self.signer.sign_calldata("0xdeadbeef", nonce=1)
        assert result["calldata"] == "0xdeadbeef"

    def test_connection_id_is_keccak256_of_calldata(self):
        """connectionId = keccak256(calldata), matching SDK behavior."""
        from web3 import Web3
        result = self.signer.sign_calldata("0xdeadbeef", nonce=1)
        expected = "0x" + Web3.keccak(hexstr="0xdeadbeef").hex()
        assert result["message"]["connectionId"] == expected

    def test_sign_calldatas_nonces_are_timestamp_based(self):
        """Nonces should be base_nonce + index (timestamp-based)."""
        before = int(time.time() * 1000) * 1000
        results = self.signer.sign_calldatas(["0xaa", "0xbb", "0xcc"])
        after = int(time.time() * 1000) * 1000

        assert len(results) == 3
        n0 = int(results[0]["message"]["nonce"])
        n1 = int(results[1]["message"]["nonce"])
        n2 = int(results[2]["message"]["nonce"])
        # Sequential with +1 offset
        assert n1 == n0 + 1
        assert n2 == n0 + 2
        # Timestamp-based range
        assert before <= n0 <= after

    def test_different_calldatas_get_different_connection_ids(self):
        results = self.signer.sign_calldatas(["0xaa", "0xbb"])
        cid_0 = results[0]["message"]["connectionId"]
        cid_1 = results[1]["message"]["connectionId"]
        assert cid_0 != cid_1

    def test_signature_is_0x_prefixed_hex(self):
        result = self.signer.sign_calldata("0xdeadbeef", nonce=1)
        sig = result["signature"]
        assert sig.startswith("0x")
        bytes.fromhex(sig[2:])  # Should not raise

    def test_connection_id_is_32_bytes_hex(self):
        result = self.signer.sign_calldata("0xdeadbeef", nonce=1)
        cid = result["message"]["connectionId"]
        assert cid.startswith("0x")
        assert len(bytes.fromhex(cid[2:])) == 32
