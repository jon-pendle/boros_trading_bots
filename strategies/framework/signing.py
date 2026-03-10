"""
EIP-712 signing for Boros agent transactions.

Flow:
  1. Get calldata from /v4/calldata/place-order or /open-api/v1/calldata/place-orders
  2. Pack account identifier (root_address + account_id)
  3. Sign EIP-712 typed data with agent private key
  4. Submit signed data to POST /v2/agent/bulk-direct-call

References:
  - https://docs.pendle.finance/boros-dev/Backend/agent
  - https://github.com/pendle-finance/boros-api-examples
"""
import logging
import time
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

logger = logging.getLogger(__name__)

# Boros Router on Arbitrum One
BOROS_ROUTER_ADDRESS = "0x8080808080daB95eFED788a9214e400ba552DEf6"
ARBITRUM_CHAIN_ID = 42161

# EIP-712 domain for Boros Router
EIP712_DOMAIN = {
    "name": "Pendle Boros Router",
    "version": "1.0",
    "chainId": ARBITRUM_CHAIN_ID,
    "verifyingContract": BOROS_ROUTER_ADDRESS,
}

# EIP-712 types for agent execution message
# Must match contract: IRouterEventsAndTypes.PendleSignTx
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "PendleSignTx": [
        {"name": "account", "type": "bytes21"},
        {"name": "connectionId", "type": "bytes32"},
        {"name": "nonce", "type": "uint64"},
    ],
}


def pack_account(root_address: str, account_id: int = 0) -> bytes:
    """
    Pack root address + account ID into a 21-byte identifier.
    Matches SDK AccountLib.pack(): (root << 8) | accountId.
    Format: root_address (20 bytes) left-shifted by 8 bits + accountId (1 byte) = 21 bytes.

    For EIP-712 signing (bytes32), eth-account right-pads to 32 bytes automatically.
    For API payload, use the raw 21-byte hex string.
    """
    root_int = int(root_address, 16)
    account_int = (root_int << 8) | (account_id & 0xFF)
    return account_int.to_bytes(21, byteorder="big")


def derive_cross_market_acc(root_address: str, token_id: int, account_id: int = 0) -> str:
    """
    Derive the cross-margin marketAcc address for API calldata endpoints.
    Format: 0x + root_address(20 bytes) + accountId_tokenId(3 bytes) + 0xffffff(3 bytes)

    The 3-byte accountId_tokenId encodes: high byte = accountId, low 2 bytes = tokenId.
    For accountId=0, tokenId=2 (WETH): 0x000002.
    The trailing 0xffffff indicates cross-margin mode.
    """
    addr_hex = root_address.lower().replace("0x", "")
    # 3 bytes: accountId (1 byte) + tokenId (2 bytes)
    acc_token = (account_id << 16) | token_id
    acc_token_hex = f"{acc_token:06x}"
    cross_marker = "ffffff"
    return f"0x{addr_hex}{acc_token_hex}{cross_marker}"


class AgentSigner:
    """Signs Boros transactions using an agent private key (EIP-712)."""

    def __init__(self, agent_private_key: str, root_address: str,
                 account_id: int = 0):
        self.account = Account.from_key(agent_private_key)
        self.agent_address = self.account.address
        self.root_address = root_address
        self.account_id = account_id
        self.packed_account = pack_account(root_address, account_id)

        logger.info(
            "AgentSigner initialized: agent=%s root=%s",
            self.agent_address, self.root_address,
        )

    def sign_calldata(self, calldata: str, nonce: int) -> dict:
        """
        Sign a single calldata with EIP-712.

        Matches SDK bulkSignWithAgentV2:
          - connectionId = keccak256(calldata)
          - nonce = timestamp-based (provided by caller)

        Returns dict ready for bulk-direct-call submission:
          {agent, message: {account, connectionId, nonce}, signature, calldata}
        """
        # connectionId = keccak256(calldata), matching SDK behavior
        connection_id = bytes(Web3.keccak(hexstr=calldata))

        message = {
            "account": self.packed_account,
            "connectionId": connection_id,
            "nonce": nonce,
        }

        # Sign EIP-712 typed data
        signable = encode_typed_data(
            domain_data=EIP712_DOMAIN,
            message_types={"PendleSignTx": EIP712_TYPES["PendleSignTx"]},
            message_data=message,
        )
        signed = self.account.sign_message(signable)

        return {
            "agent": self.agent_address,
            "message": {
                "account": "0x" + self.packed_account.hex(),
                "connectionId": "0x" + connection_id.hex(),
                "nonce": str(nonce),
            },
            "signature": "0x" + signed.signature.hex(),
            "calldata": calldata,
        }

    def sign_calldatas(self, calldatas: list[str]) -> list[dict]:
        """
        Sign multiple calldatas for bulk submission.
        Nonce = Date.now() * 1000 + index, matching SDK bulkSignWithAgentV2.
        """
        base_nonce = int(time.time() * 1000) * 1000  # milliseconds * 1000
        return [
            self.sign_calldata(cd, nonce=base_nonce + i)
            for i, cd in enumerate(calldatas)
        ]
