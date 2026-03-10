"""
Password-protected keystore for agent private key.

Uses Ethereum standard keystore format (scrypt encrypted JSON).
The private key is never stored in plaintext on disk.

Usage:
  # Encrypt key (one-time setup)
  python -m strategies.framework.keystore encrypt

  # Bot loads key at startup
  key = load_agent_key()  # prompts for password or reads from env
"""
import getpass
import json
import logging
import os
import sys
from pathlib import Path
from eth_account import Account

logger = logging.getLogger(__name__)

DEFAULT_KEYSTORE_PATH = "agent_keystore.json"


def encrypt_key(private_key: str, password: str,
                path: str = DEFAULT_KEYSTORE_PATH) -> str:
    """Encrypt a private key with password and save to keystore file."""
    keystore = Account.encrypt(private_key, password)
    filepath = Path(path)
    filepath.write_text(json.dumps(keystore, indent=2))
    os.chmod(filepath, 0o600)
    return str(filepath)


def decrypt_key(password: str, path: str = DEFAULT_KEYSTORE_PATH) -> str:
    """Decrypt private key from keystore file."""
    filepath = Path(path)
    if not filepath.exists():
        raise FileNotFoundError(f"Keystore not found: {path}")
    keystore = json.loads(filepath.read_text())
    key = Account.decrypt(keystore, password)
    return "0x" + key.hex()


def load_agent_key(keystore_path: str = DEFAULT_KEYSTORE_PATH) -> str:
    """
    Load agent private key. Priority:

    1. AGENT_KEYSTORE_PASSWORD env var + keystore file (automated/docker)
    2. Interactive password prompt + keystore file (local dev)
    3. AGENT_PRIVATE_KEY env var (fallback, plaintext)
    """
    keystore_file = Path(keystore_path)

    if keystore_file.exists():
        # Try env var password first (for docker/automated)
        password = os.environ.get("AGENT_KEYSTORE_PASSWORD", "")
        if password:
            logger.info("Decrypting keystore with env password...")
            return decrypt_key(password, keystore_path)

        # Interactive prompt (local dev)
        if sys.stdin.isatty():
            password = getpass.getpass("Agent keystore password: ")
            return decrypt_key(password, keystore_path)

        raise RuntimeError(
            f"Keystore found at {keystore_path} but no password provided. "
            "Set AGENT_KEYSTORE_PASSWORD env var or run interactively."
        )

    # Fallback: plaintext env var
    key = os.environ.get("AGENT_PRIVATE_KEY", "")
    if key:
        logger.warning("Using plaintext AGENT_PRIVATE_KEY (consider using keystore)")
        return key

    raise RuntimeError(
        "No agent key found. Either:\n"
        f"  1. Create keystore: python -m strategies.framework.keystore encrypt\n"
        "  2. Set AGENT_PRIVATE_KEY env var"
    )


def main():
    """CLI for keystore management."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m strategies.framework.keystore encrypt [--path FILE]")
        print("  python -m strategies.framework.keystore verify [--path FILE]")
        sys.exit(1)

    command = sys.argv[1]
    path = DEFAULT_KEYSTORE_PATH
    if "--path" in sys.argv:
        idx = sys.argv.index("--path")
        path = sys.argv[idx + 1]

    if command == "encrypt":
        key = getpass.getpass("Agent private key (0x...): ")
        if not key.startswith("0x"):
            key = "0x" + key
        # Validate key
        acct = Account.from_key(key)
        print(f"Agent address: {acct.address}")

        pw1 = getpass.getpass("Set keystore password: ")
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Passwords don't match")
            sys.exit(1)

        filepath = encrypt_key(key, pw1, path)
        print(f"Keystore saved to: {filepath} (chmod 600)")
        print("You can now remove AGENT_PRIVATE_KEY from .env")

    elif command == "verify":
        pw = getpass.getpass("Keystore password: ")
        try:
            key = decrypt_key(pw, path)
            acct = Account.from_key(key)
            print(f"OK - Agent address: {acct.address}")
        except Exception as e:
            print(f"Failed: {e}")
            sys.exit(1)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
