"""tests/test_setup.py

Unit tests for setup.py: write_env_token.
"""

from unittest.mock import patch

import pytest
import setup


def test_write_env_token_creates_file_if_absent(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(setup, "ENV_PATH", env_file)
    setup.write_env_token("abc123")
    assert "AUTH_TOKEN=abc123" in env_file.read_text()


def test_write_env_token_updates_existing_token(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SERVER_URL=http://example.com\nAUTH_TOKEN=oldtoken\n")
    monkeypatch.setattr(setup, "ENV_PATH", env_file)
    setup.write_env_token("newtoken")
    content = env_file.read_text()
    assert "AUTH_TOKEN=newtoken" in content
    assert "AUTH_TOKEN=oldtoken" not in content
    assert "SERVER_URL=http://example.com" in content


def test_write_env_token_appends_if_key_absent(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SERVER_URL=http://example.com\n")
    monkeypatch.setattr(setup, "ENV_PATH", env_file)
    setup.write_env_token("tok456")
    assert "AUTH_TOKEN=tok456" in env_file.read_text()
