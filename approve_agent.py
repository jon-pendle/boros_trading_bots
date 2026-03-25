"""
Agent Authorization Script

Authorizes a new agent address on Boros Router via EIP-712 signature.
The agent can then trade on behalf of the root wallet (cannot withdraw).

Three modes:
  1. Direct: provide root private key (local/secure machine)
     python approve_agent.py --agent 0xAGENT --root-key 0xROOT_KEY

  2. QR: BC-UR eth-sign-request QR for air-gapped wallets (Keystone, AirGap)
     python approve_agent.py --agent 0xAGENT --root 0xROOT_ADDR --qr

  3. Manual: display EIP-712 message, paste signature back
     python approve_agent.py --agent 0xAGENT --root 0xROOT_ADDR
"""
import argparse
import json
import logging
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Boros Router on Arbitrum
ROUTER_ADDRESS = "0x8080808080daB95eFED788a9214e400ba552DEf6"
ARBITRUM_CHAIN_ID = 42161
API_BASE = "https://api.boros.finance/send-txs-bot"

EIP712_DOMAIN = {
    "name": "Pendle Boros Router",
    "version": "1.0",
    "chainId": ARBITRUM_CHAIN_ID,
    "verifyingContract": ROUTER_ADDRESS,
}

APPROVE_AGENT_MESSAGE_TYPES = {
    "ApproveAgentMessage": [
        {"name": "root", "type": "address"},
        {"name": "accountId", "type": "uint8"},
        {"name": "agent", "type": "address"},
        {"name": "expiry", "type": "uint64"},
        {"name": "nonce", "type": "uint64"},
    ],
}

# ABI for approveAgent(ApproveAgentMessage, bytes signature)
APPROVE_AGENT_ABI = [{
    "inputs": [
        {
            "components": [
                {"name": "root", "type": "address"},
                {"name": "accountId", "type": "uint8"},
                {"name": "agent", "type": "address"},
                {"name": "expiry", "type": "uint64"},
                {"name": "nonce", "type": "uint64"},
            ],
            "name": "data",
            "type": "tuple",
        },
        {"name": "signature", "type": "bytes"},
    ],
    "name": "approveAgent",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]

YEAR_SECONDS = 365 * 24 * 3600


def build_approve_message(root_address: str, agent_address: str,
                          expiry_seconds: int = YEAR_SECONDS,
                          account_id: int = 0) -> dict:
    now_s = int(time.time())
    nonce = int(time.time() * 1000) * 1000  # millis * 1000 per Boros signing spec
    return {
        "root": Web3.to_checksum_address(root_address),
        "accountId": account_id,
        "agent": Web3.to_checksum_address(agent_address),
        "expiry": now_s + expiry_seconds,
        "nonce": nonce,
    }


def sign_approve_message(message: dict, root_private_key: str) -> str:
    signable = encode_typed_data(
        domain_data=EIP712_DOMAIN,
        message_types=APPROVE_AGENT_MESSAGE_TYPES,
        message_data=message,
    )
    account = Account.from_key(root_private_key)
    signed = account.sign_message(signable)
    return "0x" + signed.signature.hex()


def encode_calldata(message: dict, signature: str) -> str:
    w3 = Web3()
    contract = w3.eth.contract(abi=APPROVE_AGENT_ABI)
    msg_tuple = (
        message["root"],
        message["accountId"],
        message["agent"],
        message["expiry"],
        message["nonce"],
    )
    return contract.encode_abi("approveAgent", [msg_tuple, bytes.fromhex(signature[2:])])


def submit_approval(calldata: str) -> dict:
    resp = requests.post(
        f"{API_BASE}/v1/agent/approve",
        json={"approveAgentCalldata": calldata, "skipReceipt": False},
        timeout=30,
    )
    if resp.status_code in (200, 201):
        return resp.json()
    raise RuntimeError(f"Approval failed: HTTP {resp.status_code} - {resp.text[:300]}")


def mode_direct(args):
    """Sign with root key directly."""
    root_account = Account.from_key(args.root_key)
    message = build_approve_message(
        root_address=root_account.address,
        agent_address=args.agent,
        expiry_seconds=args.expiry_days * 86400,
    )

    print(f"\n  Root:    {root_account.address}")
    print(f"  Agent:   {message['agent']}")
    print(f"  Expiry:  {args.expiry_days} days")

    signature = sign_approve_message(message, args.root_key)
    calldata = encode_calldata(message, signature)

    print("\nSubmitting approval...")
    result = submit_approval(calldata)
    print(f"Done! {json.dumps(result, indent=2)}")


def mode_manual(args):
    """Generate EIP-712 message, wait for signature paste."""
    message = build_approve_message(
        root_address=args.root,
        agent_address=args.agent,
        expiry_seconds=args.expiry_days * 86400,
    )

    typed_data = _build_eip712_typed_data(
        APPROVE_AGENT_MESSAGE_TYPES, "ApproveAgentMessage", message)

    print(f"\n  Root:    {message['root']}")
    print(f"  Agent:   {message['agent']}")
    print(f"  Expiry:  {args.expiry_days} days")
    print(f"\nEIP-712 message to sign:\n")
    print(json.dumps(typed_data, indent=2))

    signature = input("\nPaste signature (0x...): ").strip()
    if not signature.startswith("0x") or len(signature) < 130:
        print("Invalid signature format")
        sys.exit(1)

    calldata = encode_calldata(message, signature)

    print("\nSubmitting approval...")
    result = submit_approval(calldata)
    print(f"Done! {json.dumps(result, indent=2)}")


def _build_eip712_typed_data(message_types: dict, primary_type: str,
                              message: dict) -> dict:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            **message_types,
        },
        "primaryType": primary_type,
        "domain": {
            "name": EIP712_DOMAIN["name"],
            "version": EIP712_DOMAIN["version"],
            "chainId": ARBITRUM_CHAIN_ID,
            "verifyingContract": ROUTER_ADDRESS,
        },
        "message": {k: str(v) if isinstance(v, int) else v for k, v in message.items()},
    }


def mode_qr(args):
    """Generate BC-UR eth-sign-request QR for air-gapped wallets."""
    from bc_ur import generate_eth_sign_request_ur_multi, show_ur_qr

    message = build_approve_message(
        root_address=args.root,
        agent_address=args.agent,
        expiry_seconds=args.expiry_days * 86400,
    )
    typed_data = _build_eip712_typed_data(
        APPROVE_AGENT_MESSAGE_TYPES, "ApproveAgentMessage", message)

    print(f"\n  Root:    {message['root']}")
    print(f"  Agent:   {message['agent']}")
    print(f"  Expiry:  {args.expiry_days} days")

    xfp = int(args.xfp, 16) if args.xfp else 0
    if not args.xfp:
        logger.warning("WARNING: --xfp not set. AirGap Vault needs master fingerprint to match wallet.")

    ur_parts = generate_eth_sign_request_ur_multi(
        typed_data=typed_data,
        signer_address=message["root"],
        chain_id=ARBITRUM_CHAIN_ID,
        derivation_path=args.derivation_path,
        source_fingerprint=xfp,
        max_fragment_len=250,
    )

    print(f"\n  APPROVE AGENT — {len(ur_parts)} QR code(s)\n")
    for i, part in enumerate(ur_parts):
        show_ur_qr(part, f"[{i+1}/{len(ur_parts)}]")

    print("\n1. Scan QR with your air-gapped wallet")
    print("2. Approve the signing request")
    print("3. Paste the signature below")

    signature = input("\nPaste signature (0x... or ur:eth-signature/...): ").strip()
    if signature.lower().startswith("ur:eth-signature/"):
        from bc_ur import decode_eth_signature_ur
        signature = decode_eth_signature_ur(signature)
        print(f"  Decoded signature: {signature[:20]}...{signature[-8:]}")
    elif not signature.startswith("0x") or len(signature) < 130:
        print("Invalid signature format")
        sys.exit(1)

    calldata = encode_calldata(message, signature)

    print("\nSubmitting approval...")
    result = submit_approval(calldata)
    print(f"Done! {json.dumps(result, indent=2)}")


def main():
    parser = argparse.ArgumentParser(description="Authorize Boros Agent")
    parser.add_argument("--agent", required=True, help="Agent address to authorize")
    parser.add_argument("--expiry-days", type=int, default=365, help="Expiry in days (default: 365)")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-key", help="Root wallet private key (direct mode)")
    group.add_argument("--root", help="Root wallet address (manual/QR mode)")
    parser.add_argument("--qr", action="store_true", help="BC-UR QR mode for air-gapped wallets")
    parser.add_argument("--derivation-path", default="m/44'/60'/0'/0/0",
                        help="HD derivation path (default: m/44'/60'/0'/0/0)")
    parser.add_argument("--xfp", default="",
                        help="Master fingerprint hex (e.g. 12345678). "
                             "Required for QR mode. Find in AirGap Vault account details.")

    args = parser.parse_args()

    if args.root_key:
        mode_direct(args)
    elif args.qr:
        if not args.root:
            parser.error("--root required for QR mode")
        mode_qr(args)
    else:
        mode_manual(args)


if __name__ == "__main__":
    main()
