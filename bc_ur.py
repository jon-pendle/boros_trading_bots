"""
BC-UR eth-sign-request generator for air-gapped wallet signing.

Generates a QR code containing an eth-sign-request that can be scanned by
Keystone, AirGap Vault, or other BC-UR compatible wallets.

Spec references:
  - EIP-4527: QR Code data for Ethereum transactions
  - BC-UR: https://github.com/BlockchainCommons/bc-ur
  - Bytewords: https://github.com/BlockchainCommons/bc-bytewords
  - ur-registry-eth: https://github.com/KeystoneHQ/ur-registry-eth

eth-sign-request CBOR map keys:
  1: request_id    (UUID bytes, CBOR tag 37)
  2: sign_data     (bytes - JSON string for EIP-712)
  3: data_type     (int: 1=legacy_tx, 2=eip712_typed_data, 3=raw, 4=typed_tx)
  4: chain_id      (int, optional)
  5: derivation    (crypto-keypath, CBOR tag 304, required for wallet matching)
  6: address       (bytes, optional)
  7: origin        (string, optional)
"""
import io
import json
import logging
import math
import os
import sys
import time
import uuid
import zlib
from typing import List, Optional

import cbor2
import qrcode

logger = logging.getLogger(__name__)

# CBOR tags
TAG_UUID = 37
TAG_CRYPTO_KEYPATH = 304  # crypto-keypath tag per BC-UR registry

# eth-sign-request data_type values
# 1=legacy tx, 2=EIP-712 typed data, 3=raw bytes, 4=EIP-2718 typed tx
DATA_TYPE_EIP712 = 2

# Official BC-UR Bytewords table (256 words, from BlockchainCommons/bc-ur)
# Each byte (0-255) maps to a unique 4-letter word with unique first+last pair.
BYTEWORDS = [
    "able", "acid", "also", "apex", "aqua", "arch", "atom", "aunt",
    "away", "axis", "back", "bald", "barn", "belt", "beta", "bias",
    "blue", "body", "brag", "brew", "bulb", "buzz", "calm", "cash",
    "cats", "chef", "city", "claw", "code", "cola", "cook", "cost",
    "crux", "curl", "cusp", "cyan", "dark", "data", "days", "deli",
    "dice", "diet", "door", "down", "draw", "drop", "drum", "dull",
    "duty", "each", "easy", "echo", "edge", "epic", "even", "exam",
    "exit", "eyes", "fact", "fair", "fern", "figs", "film", "fish",
    "fizz", "flap", "flew", "flux", "foxy", "free", "frog", "fuel",
    "fund", "gala", "game", "gear", "gems", "gift", "girl", "glow",
    "good", "gray", "grim", "guru", "gush", "gyro", "half", "hang",
    "hard", "hawk", "heat", "help", "high", "hill", "holy", "hope",
    "horn", "huts", "iced", "idea", "idle", "inch", "inky", "into",
    "iris", "iron", "item", "jade", "jazz", "join", "jolt", "jowl",
    "judo", "jugs", "jump", "junk", "jury", "keep", "keno", "kept",
    "keys", "kick", "kiln", "king", "kite", "kiwi", "knob", "lamb",
    "lava", "lazy", "leaf", "legs", "liar", "limp", "lion", "list",
    "logo", "loud", "love", "luau", "luck", "lung", "main", "many",
    "math", "maze", "memo", "menu", "meow", "mild", "mint", "miss",
    "monk", "nail", "navy", "need", "news", "next", "noon", "note",
    "numb", "obey", "oboe", "omit", "onyx", "open", "oval", "owls",
    "paid", "part", "peck", "play", "plus", "poem", "pool", "pose",
    "puff", "puma", "purr", "quad", "quiz", "race", "ramp", "real",
    "redo", "rich", "road", "rock", "roof", "ruby", "ruin", "runs",
    "rust", "safe", "saga", "scar", "sets", "silk", "skew", "slot",
    "soap", "solo", "song", "stub", "surf", "swan", "taco", "task",
    "taxi", "tent", "tied", "time", "tiny", "toil", "tomb", "toys",
    "trip", "tuna", "twin", "ugly", "undo", "unit", "urge", "user",
    "vast", "very", "veto", "vial", "vibe", "view", "visa", "void",
    "vows", "wall", "wand", "warm", "wasp", "wave", "waxy", "webs",
    "what", "when", "whiz", "wolf", "work", "yank", "yawn", "yell",
    "yoga", "yurt", "zaps", "zero", "zest", "zinc", "zone", "zoom",
]

# Reverse lookup: minimal code (first+last letter) -> byte value
_MINIMAL_LOOKUP = {w[0] + w[-1]: i for i, w in enumerate(BYTEWORDS)}


def _bytewords_encode(data: bytes) -> str:
    """Encode bytes to bytewords minimal format (first+last letter of each word).
    Appends CRC32 checksum per BC-UR spec."""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    data_with_crc = data + crc.to_bytes(4, "big")
    return "".join(BYTEWORDS[b][0] + BYTEWORDS[b][-1] for b in data_with_crc)


def _bytewords_decode(encoded: str) -> bytes:
    """Decode bytewords minimal format back to bytes, verify CRC32."""
    encoded = encoded.lower()
    result = []
    for i in range(0, len(encoded), 2):
        code = encoded[i:i + 2]
        if code not in _MINIMAL_LOOKUP:
            raise ValueError(f"Invalid bytewords code at pos {i}: {code}")
        result.append(_MINIMAL_LOOKUP[code])
    data_with_crc = bytes(result)
    data = data_with_crc[:-4]
    expected_crc = int.from_bytes(data_with_crc[-4:], "big")
    actual_crc = zlib.crc32(data) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ValueError("Bytewords CRC32 checksum mismatch")
    return data


def encode_ur(ur_type: str, cbor_data: bytes) -> str:
    """Encode CBOR data as a single-part UR string: ur:<type>/<bytewords-minimal>"""
    encoded = _bytewords_encode(cbor_data)
    return f"ur:{ur_type}/{encoded}"


def encode_ur_multi(ur_type: str, cbor_data: bytes,
                    max_fragment_len: int = 250) -> List[str]:
    """Encode CBOR data as multi-part UR strings (BC-UR fountain code format).

    Each part: ur:<type>/<seqNum>-<seqLen>/<bytewords-of-cbor-encoded-part>

    The inner CBOR for each part is: [seqNum, seqLen, messageLen, checksum, fragment]
    All fragments are zero-padded to equal length per @ngraveio/bc-ur spec.
    Compatible with @ngraveio/bc-ur URDecoder.
    """
    msg_len = len(cbor_data)
    checksum = zlib.crc32(cbor_data) & 0xFFFFFFFF
    frag_len = max_fragment_len
    seq_len = math.ceil(msg_len / frag_len)

    if seq_len <= 1:
        return [encode_ur(ur_type, cbor_data)]

    # Split into equal-length fragments (last one zero-padded)
    fragments = []
    for i in range(seq_len):
        start = i * frag_len
        frag = cbor_data[start:start + frag_len]
        if len(frag) < frag_len:
            frag = frag + b"\x00" * (frag_len - len(frag))
        fragments.append(frag)

    parts = []
    for i, frag in enumerate(fragments):
        seq_num = i + 1
        part_cbor = cbor2.dumps([seq_num, seq_len, msg_len, checksum, frag])
        encoded = _bytewords_encode(part_cbor)
        parts.append(f"ur:{ur_type}/{seq_num}-{seq_len}/{encoded}")

    return parts


def build_crypto_keypath(path: str = "m/44'/60'/0'/0/0",
                         source_fingerprint: int = 0) -> cbor2.CBORTag:
    """Build a crypto-keypath CBOR structure (tag 304).

    Path components are encoded as a flat array of [index, hardened] pairs.
    Example: m/44'/60'/0'/0/0 -> [44, true, 60, true, 0, true, 0, false, 0, false]
    """
    components = []
    parts = path.replace("m/", "").split("/")
    for part in parts:
        hardened = part.endswith("'") or part.endswith("h")
        index = int(part.rstrip("'h"))
        components.append(index)
        components.append(hardened)

    keypath_map = {1: components}
    if source_fingerprint:
        keypath_map[2] = source_fingerprint

    return cbor2.CBORTag(TAG_CRYPTO_KEYPATH, keypath_map)


def build_eth_sign_request(
    sign_data: bytes,
    data_type: int = DATA_TYPE_EIP712,
    chain_id: int = 42161,
    derivation_path: str = "m/44'/60'/0'/0/0",
    source_fingerprint: int = 0,
    address: Optional[bytes] = None,
    origin: str = "Boros Trade Bot",
    request_id: Optional[bytes] = None,
) -> bytes:
    """Build CBOR-encoded eth-sign-request per EIP-4527 / ur-registry-eth."""
    if request_id is None:
        request_id = uuid.uuid4().bytes

    cbor_map = {
        1: cbor2.CBORTag(TAG_UUID, request_id),
        2: sign_data,
        3: data_type,
        4: chain_id,
        5: build_crypto_keypath(derivation_path, source_fingerprint),
    }

    if address is not None:
        cbor_map[6] = address

    if origin:
        cbor_map[7] = origin

    return cbor2.dumps(cbor_map)


def generate_eth_sign_request_ur(
    typed_data: dict,
    signer_address: str,
    chain_id: int = 42161,
    derivation_path: str = "m/44'/60'/0'/0/0",
    source_fingerprint: int = 0,
    origin: str = "Boros Trade Bot",
) -> str:
    """
    Generate a UR-encoded eth-sign-request string.

    Format: ur:eth-sign-request/<bytewords-minimal-encoded-cbor>

    For EIP-712 typed data, sign_data is the JSON string as bytes.
    data_type=2 (EIP-712 typed data) tells the wallet to parse and display it.
    """
    sign_data = json.dumps(typed_data, separators=(",", ":")).encode("utf-8")
    address_bytes = bytes.fromhex(signer_address.lower().replace("0x", ""))

    cbor_data = build_eth_sign_request(
        sign_data=sign_data,
        data_type=DATA_TYPE_EIP712,
        chain_id=chain_id,
        derivation_path=derivation_path,
        source_fingerprint=source_fingerprint,
        address=address_bytes,
        origin=origin,
    )

    return encode_ur("eth-sign-request", cbor_data)


def generate_eth_sign_request_ur_multi(
    typed_data: dict,
    signer_address: str,
    chain_id: int = 42161,
    derivation_path: str = "m/44'/60'/0'/0/0",
    source_fingerprint: int = 0,
    origin: str = "Boros Trade Bot",
    max_fragment_len: int = 250,
) -> List[str]:
    """Generate multi-part UR-encoded eth-sign-request strings for animated QR."""
    sign_data = json.dumps(typed_data, separators=(",", ":")).encode("utf-8")
    address_bytes = bytes.fromhex(signer_address.lower().replace("0x", ""))

    cbor_data = build_eth_sign_request(
        sign_data=sign_data,
        data_type=DATA_TYPE_EIP712,
        chain_id=chain_id,
        derivation_path=derivation_path,
        source_fingerprint=source_fingerprint,
        address=address_bytes,
        origin=origin,
    )

    return encode_ur_multi("eth-sign-request", cbor_data, max_fragment_len)


def _render_qr_string(data: str) -> str:
    """Render a QR code to a string (for terminal display)."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    f = io.StringIO()
    qr.print_ascii(out=f, invert=True)
    return f.getvalue()


def show_ur_qr(ur_string: str, label: str = ""):
    """Display a UR string as a QR code in the terminal."""
    if label:
        print(f"\n{label}")
    print(_render_qr_string(ur_string.upper()))


def show_animated_ur_qr(ur_parts: List[str], label: str = "",
                        interval: float = 0.5):
    """Display multi-part UR as animated QR codes cycling in terminal.

    Press Ctrl+C to stop.
    """
    if len(ur_parts) == 1:
        show_ur_qr(ur_parts[0], label)
        return

    # Pre-render all QR frames
    frames = []
    for part in ur_parts:
        frames.append(_render_qr_string(part.upper()))

    frame_height = frames[0].count("\n") + 1

    print(f"\n{label}" if label else "")
    print(f"Animated QR: {len(ur_parts)} parts, {interval}s interval")
    print("Point camera at screen. Press Ctrl+C when done.\n")

    try:
        idx = 0
        while True:
            # Move cursor up to overwrite previous frame
            if idx > 0:
                sys.stdout.write(f"\033[{frame_height + 1}A")

            part_label = f"  [{idx + 1}/{len(ur_parts)}]"
            sys.stdout.write(part_label + "\n" + frames[idx])
            sys.stdout.flush()

            idx = (idx + 1) % len(ur_parts)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def decode_eth_signature_ur(ur_string: str) -> str:
    """Decode a ur:eth-signature/... string and return the signature as 0x-hex.

    eth-signature CBOR map keys:
      1: requestId (UUID, tag 37)
      2: signature (65 bytes: r[32] + s[32] + v[1])
      3: origin (string, optional)

    Returns: 0x-prefixed hex signature string (130 hex chars + 0x prefix)
    """
    ur_string = ur_string.strip()
    prefix = "ur:eth-signature/"
    if not ur_string.lower().startswith(prefix):
        raise ValueError(f"Not a ur:eth-signature string: {ur_string[:40]}...")

    payload = ur_string[len(prefix):]

    # Check for multi-part format: seqNum-seqLen/bytewords
    if "/" in payload:
        raise ValueError(
            "Multi-part ur:eth-signature not supported yet. "
            "Please paste the single-part UR string."
        )

    # Single-part: payload is bytewords-minimal encoded CBOR
    cbor_data = _bytewords_decode(payload)
    decoded = cbor2.loads(cbor_data)

    # Key 2 = signature bytes
    if 2 not in decoded:
        raise ValueError(f"No signature (key 2) in eth-signature CBOR. Keys: {list(decoded.keys())}")

    sig_bytes = decoded[2]
    if len(sig_bytes) != 65:
        logger.warning(f"Unexpected signature length: {len(sig_bytes)} (expected 65)")

    return "0x" + sig_bytes.hex()
