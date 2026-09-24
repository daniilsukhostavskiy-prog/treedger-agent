"""
agent/tests/test_config_store.py — every `<behavior>` bullet for
`agent/config_store.py` (39-14-PLAN.md Task 1).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import config_store


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config_store at a throwaway directory for every test in this file."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("HOME", raising=False)
    return tmp_path


def test_save_token_then_load_token_round_trips() -> None:
    config_store.save_token("tok_abc123")
    assert config_store.load_token() == "tok_abc123"


def test_clear_token_makes_load_token_return_none() -> None:
    config_store.save_token("tok_abc123")
    config_store.clear_token()
    assert config_store.load_token() is None


def test_save_base_url_then_load_base_url_round_trips() -> None:
    config_store.save_base_url("https://treedger.com")
    assert config_store.load_base_url() == "https://treedger.com"


def test_config_file_contains_exactly_token_and_base_url_keys() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")

    with config_store.config_path().open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert set(data.keys()) == {"token", "base_url"}
    # No account, no login, no server name and no password ever lands in here.
    assert "account" not in data
    assert "login" not in data
    assert "server" not in data
    assert "password" not in data
    assert "investorPassword" not in data


def test_load_token_on_missing_file_returns_none() -> None:
    assert not config_store.config_path().exists()
    assert config_store.load_token() is None


def test_load_token_on_corrupt_file_returns_none_not_raises() -> None:
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json at all", encoding="utf-8")

    assert config_store.load_token() is None


def test_load_base_url_on_corrupt_file_returns_none_not_raises() -> None:
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not even close to json", encoding="utf-8")

    assert config_store.load_base_url() is None


def test_config_file_is_not_json_array_or_scalar_at_top_level() -> None:
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert config_store.load_token() is None
    assert config_store.load_base_url() is None


def test_save_token_preserves_existing_base_url() -> None:
    config_store.save_base_url("https://treedger.com")
    config_store.save_token("tok_abc123")

    assert config_store.load_base_url() == "https://treedger.com"
    assert config_store.load_token() == "tok_abc123"


def test_save_token_overwrites_previous_token() -> None:
    config_store.save_token("tok_first")
    config_store.save_token("tok_second")
    assert config_store.load_token() == "tok_second"


def test_config_path_lives_under_app_data_not_beside_the_program() -> None:
    path = config_store.config_path()
    agent_dir = Path(__file__).resolve().parent.parent
    assert agent_dir not in path.parents
    assert path != agent_dir / "config.json"
