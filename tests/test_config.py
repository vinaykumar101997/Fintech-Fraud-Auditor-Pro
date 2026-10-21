"""config.py loads the repo-root .env itself, without overriding the shell.

Offline: each test writes its own temporary .env and never reads the real one
(conftest.py sets FFAV_SKIP_DOTENV=1 for the whole suite).
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

KEYS = ("MODEL_PROVIDER", "VERTEX_CHAT_MODEL")


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A temporary .env, with the keys it sets cleared and restored afterwards."""
    path = tmp_path / ".env"
    path.write_text("MODEL_PROVIDER=vertex\nVERTEX_CHAT_MODEL=gemini-test\n", encoding="utf-8")
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    yield path


@pytest.fixture
def reload_config():
    """Reload config after the test, once monkeypatch has restored os.environ
    (requested first, so it tears down last).

    FFAV_SKIP_DOTENV stays set from conftest, so the reload never reads the
    developer's real .env.
    """
    yield
    importlib.reload(config)


def test_env_file_values_reach_config(reload_config, env_file, monkeypatch):
    pytest.importorskip("dotenv")
    with monkeypatch.context() as mp:
        mp.delenv("FFAV_SKIP_DOTENV")
        config.load_env_file(env_file)
    # Reload with the opt-out back on: the values come from the file loaded above.
    importlib.reload(config)
    assert config.MODEL_PROVIDER == "vertex"
    assert config.VERTEX_CHAT_MODEL == "gemini-test"


def test_already_set_variables_win_over_env_file(env_file, monkeypatch):
    pytest.importorskip("dotenv")
    monkeypatch.delenv("FFAV_SKIP_DOTENV")
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    config.load_env_file(env_file)
    assert os.environ["MODEL_PROVIDER"] == "bedrock"
    assert os.environ["VERTEX_CHAT_MODEL"] == "gemini-test"


def test_opt_out_skips_loading(env_file, monkeypatch):
    monkeypatch.setenv("FFAV_SKIP_DOTENV", "1")
    config.load_env_file(env_file)
    assert "MODEL_PROVIDER" not in os.environ
    assert "VERTEX_CHAT_MODEL" not in os.environ


def test_missing_dotenv_package_is_skipped_silently(env_file, monkeypatch):
    monkeypatch.delenv("FFAV_SKIP_DOTENV")
    monkeypatch.setitem(sys.modules, "dotenv", None)  # makes the import raise ImportError
    config.load_env_file(env_file)
    assert "MODEL_PROVIDER" not in os.environ
