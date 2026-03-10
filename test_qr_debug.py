"""
Minimal BC-UR eth-sign-request debug tool.

Generates the simplest possible sign request to test AirGap Vault scanning.
Starts with the most basic request and progressively adds complexity.
"""
import sys
import json
from bc_ur import (
    build_eth_sign_request, encode_ur, encode_ur_multi,
    show_ur_qr, _bytewords_decode, DATA_TYPE_EIP712,
)
import cbor2


def test_single_raw_message(xfp: int):
    """Test 1: Simplest possible - raw personal message (data_type=3)."""
    print("=" * 60)
    print("TEST 1: Personal message (data_type=3, single QR)")
    print("=" * 60)

    cbor_data = build_eth_sign_request(
        sign_data=b"hello",
        data_type=3,  # personalMessage
        chain_id=1,
        derivation_path="m/44'/60'/0'/0/0",
        source_fingerprint=xfp,
        origin="debug",
    )

    ur = encode_ur("eth-sign-request", cbor_data)
    print(f"CBOR: {len(cbor_data)} bytes")
    print(f"UR:   {len(ur)} chars")
    show_ur_qr(ur)
    input("Press Enter after scanning (or 'skip')... ")


def test_single_typed_data(xfp: int):
    """Test 2: Minimal EIP-712 typed data (single QR)."""
    print("=" * 60)
    print("TEST 2: Minimal EIP-712 typed data (data_type=2, single QR)")
    print("=" * 60)

    typed_data = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}],
            "Test": [{"name": "value", "type": "uint256"}],
        },
        "primaryType": "Test",
        "domain": {"name": "Debug"},
        "message": {"value": "42"},
    }

    sign_data = json.dumps(typed_data, separators=(",", ":")).encode("utf-8")
    cbor_data = build_eth_sign_request(
        sign_data=sign_data,
        data_type=2,  # typedData (EIP-712)
        chain_id=1,
        derivation_path="m/44'/60'/0'/0/0",
        source_fingerprint=xfp,
        origin="debug",
    )

    ur = encode_ur("eth-sign-request", cbor_data)
    print(f"sign_data: {len(sign_data)} bytes")
    print(f"CBOR: {len(cbor_data)} bytes")
    print(f"UR:   {len(ur)} chars")
    show_ur_qr(ur)
    input("Press Enter after scanning (or 'skip')... ")


def test_multi_typed_data(xfp: int):
    """Test 3: Full ApproveAgent typed data (multi-part QR)."""
    print("=" * 60)
    print("TEST 3: Full ApproveAgent EIP-712 (multi-part QR)")
    print("=" * 60)

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "ApproveAgentMessage": [
                {"name": "root", "type": "address"},
                {"name": "accountId", "type": "uint8"},
                {"name": "agent", "type": "address"},
                {"name": "expiry", "type": "uint64"},
                {"name": "nonce", "type": "uint64"},
            ],
        },
        "primaryType": "ApproveAgentMessage",
        "domain": {
            "name": "Pendle Boros Router",
            "version": "1.0",
            "chainId": 42161,
            "verifyingContract": "0x8080808080daB95eFED788a9214e400ba552DEf6",
        },
        "message": {
            "root": "0x0000000000000000000000000000000000000001",
            "accountId": 0,
            "agent": "0x0000000000000000000000000000000000000002",
            "expiry": "1700000000",
            "nonce": "1699999000",
        },
    }

    sign_data = json.dumps(typed_data, separators=(",", ":")).encode("utf-8")
    cbor_data = build_eth_sign_request(
        sign_data=sign_data,
        data_type=2,
        chain_id=42161,
        derivation_path="m/44'/60'/0'/0/0",
        source_fingerprint=xfp,
        origin="Boros Trade Bot",
    )

    parts = encode_ur_multi("eth-sign-request", cbor_data, max_fragment_len=250)
    print(f"sign_data: {len(sign_data)} bytes")
    print(f"CBOR: {len(cbor_data)} bytes")
    print(f"Parts: {len(parts)}")
    for i, p in enumerate(parts):
        print(f"\n  [{i+1}/{len(parts)}]")
        show_ur_qr(p)
    input("Press Enter after scanning... ")


def dump_cbor(xfp: int):
    """Dump CBOR hex for external verification."""
    print("=" * 60)
    print("CBOR HEX DUMP (paste into online CBOR decoder to verify)")
    print("=" * 60)

    cbor_data = build_eth_sign_request(
        sign_data=b"hello",
        data_type=3,
        chain_id=1,
        derivation_path="m/44'/60'/0'/0/0",
        source_fingerprint=xfp,
        origin="debug",
    )
    print(f"CBOR hex: {cbor_data.hex()}")

    decoded = cbor2.loads(cbor_data)
    print(f"Decoded:")
    for k, v in sorted(decoded.items()):
        print(f"  key {k}: {repr(v)[:100]}")

    ur = encode_ur("eth-sign-request", cbor_data)
    print(f"\nFull UR string:")
    print(ur)


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_qr_debug.py <xfp_hex> [test_num]")
        print("  xfp_hex:  master fingerprint (e.g. 73c5da0a)")
        print("  test_num: 1=personal_msg, 2=typed_data, 3=multi_part, 4=dump")
        print("\nExample: python test_qr_debug.py 73c5da0a")
        sys.exit(1)

    xfp = int(sys.argv[1], 16)
    test_num = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    print(f"XFP: {xfp:08x}")

    if test_num == 0:
        # Run all tests
        test_single_raw_message(xfp)
        test_single_typed_data(xfp)
        test_multi_typed_data(xfp)
        dump_cbor(xfp)
    elif test_num == 1:
        test_single_raw_message(xfp)
    elif test_num == 2:
        test_single_typed_data(xfp)
    elif test_num == 3:
        test_multi_typed_data(xfp)
    elif test_num == 4:
        dump_cbor(xfp)


if __name__ == "__main__":
    main()
