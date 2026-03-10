"""Tests for password-protected keystore."""
import os
import pytest
from pathlib import Path
from strategies.framework.keystore import encrypt_key, decrypt_key, load_agent_key

TEST_KEY = "0x" + "ab" * 32
TEST_PASSWORD = "test_password_123"


class TestKeystore:
    def test_encrypt_decrypt_roundtrip(self, tmp_path):
        path = str(tmp_path / "test_keystore.json")
        encrypt_key(TEST_KEY, TEST_PASSWORD, path)
        result = decrypt_key(TEST_PASSWORD, path)
        assert result == TEST_KEY

    def test_file_permissions(self, tmp_path):
        path = str(tmp_path / "test_keystore.json")
        encrypt_key(TEST_KEY, TEST_PASSWORD, path)
        stat = os.stat(path)
        assert oct(stat.st_mode)[-3:] == "600"

    def test_wrong_password_fails(self, tmp_path):
        path = str(tmp_path / "test_keystore.json")
        encrypt_key(TEST_KEY, TEST_PASSWORD, path)
        with pytest.raises(Exception):
            decrypt_key("wrong_password", path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            decrypt_key(TEST_PASSWORD, str(tmp_path / "nonexistent.json"))

    def test_load_from_keystore_env_password(self, tmp_path, monkeypatch):
        path = str(tmp_path / "ks.json")
        encrypt_key(TEST_KEY, TEST_PASSWORD, path)
        monkeypatch.setenv("AGENT_KEYSTORE_PASSWORD", TEST_PASSWORD)
        monkeypatch.delenv("AGENT_PRIVATE_KEY", raising=False)
        result = load_agent_key(path)
        assert result == TEST_KEY

    def test_load_fallback_to_env_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_PRIVATE_KEY", TEST_KEY)
        monkeypatch.delenv("AGENT_KEYSTORE_PASSWORD", raising=False)
        # No keystore file exists at this path
        result = load_agent_key(str(tmp_path / "nonexistent.json"))
        assert result == TEST_KEY

    def test_load_no_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("AGENT_KEYSTORE_PASSWORD", raising=False)
        with pytest.raises(RuntimeError):
            load_agent_key(str(tmp_path / "nonexistent.json"))
