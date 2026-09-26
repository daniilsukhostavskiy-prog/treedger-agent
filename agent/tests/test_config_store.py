"""
agent/tests/test_config_store.py — the test coverage for
`agent/config_store.py`, including
DPAPI-backed token storage.
"""
from __future__ import annotations

import base64
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


@pytest.fixture(autouse=True)
def _fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Substitute `config_store.dpapi.protect`/`unprotect` with a reversible, NON-DPAPI
    stand-in (byte-reversal) so every test in this file runs on any platform,
    regardless of whether real DPAPI is available in the current environment.

    This substitution is NOT sufficient proof on its own that DPAPI itself works —
    it proves only the LOGIC around the encryption layer (config_store's own branching),
    never that the real Win32 DPAPI call itself works. That is why
    `agent/tests/test_dpapi.py`'s real-syscall round-trip test exists alongside this
    fixture, not instead of it: this fixture and that test cover two different
    claims, and neither substitutes for the other.
    """

    def _fake_protect(plaintext: bytes) -> bytes:
        return plaintext[::-1]

    def _fake_unprotect(ciphertext: bytes) -> bytes:
        return ciphertext[::-1]

    monkeypatch.setattr(config_store.dpapi, "protect", _fake_protect)
    monkeypatch.setattr(config_store.dpapi, "unprotect", _fake_unprotect)


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


def test_config_file_keys_are_a_subset_of_the_allow_list() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")

    with config_store.config_path().open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert set(data.keys()) <= config_store.ALLOWED_KEYS
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


# ---------------------------------------------------------------------------
# The token on disk is ciphertext, never plaintext.
# ---------------------------------------------------------------------------

def test_stored_token_value_is_not_the_plaintext_string() -> None:
    config_store.save_token("tok_abc123")

    with config_store.config_path().open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert data["token"] != "tok_abc123"


def test_load_token_returns_none_for_legacy_plaintext_token() -> None:
    """
    A Phase-39 install has a bare plaintext string under `token` (no base64, no
    encryption at all). `load_token()` must not read it, parse it, or accept it —
    it lands in the exact same `None` path as any other unreadable value.
    """
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": "tok_abc123"}), encoding="utf-8")

    assert config_store.load_token() is None


def test_load_token_returns_none_when_decryption_fails() -> None:
    """
    A `token` value that IS well-formed base64 but whose decryption fails (a
    corrupted ciphertext, or DPAPI simply refusing) must also resolve to `None`,
    never raise past this function.
    """
    def _raising_unprotect(ciphertext: bytes) -> bytes:
        raise OSError("simulated CryptUnprotectData failure")

    original = config_store.dpapi.unprotect
    config_store.dpapi.unprotect = _raising_unprotect  # type: ignore[assignment]
    try:
        path = config_store.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        well_formed_base64 = base64.b64encode(b"looks-like-ciphertext").decode("ascii")
        path.write_text(json.dumps({"token": well_formed_base64}), encoding="utf-8")

        assert config_store.load_token() is None
    finally:
        config_store.dpapi.unprotect = original  # type: ignore[assignment]


def test_load_token_returns_none_when_decrypted_bytes_are_not_valid_utf8() -> None:
    """
    Even if base64-decoding and "decryption" (the fake reversible stand-in) both
    succeed, bytes that don't decode as UTF-8 must still resolve to `None`, not
    raise a `UnicodeDecodeError` past this function.
    """
    # b"\x80\x81" is not valid UTF-8, and reversing it (the fake unprotect) yields
    # b"\x81\x80", which is also not valid UTF-8 — either order fails the same way.
    stored = base64.b64encode(b"\x80\x81").decode("ascii")
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": stored}), encoding="utf-8")

    assert config_store.load_token() is None


def test_clear_token_leaves_base_url_intact_with_ciphertext_token() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")
    config_store.clear_token()

    assert config_store.load_token() is None
    assert config_store.load_base_url() == "https://treedger.com"


def test_config_file_still_exactly_two_keys_after_ciphertext_token() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")

    with config_store.config_path().open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert set(data.keys()) == {"token", "base_url"}


# ---------------------------------------------------------------------------
# quick 260926-ieo — «Звуки MT5» preference + mute-pending marker (closed
# allow-list of four keys)
# ---------------------------------------------------------------------------


def test_load_mt5_sounds_muted_defaults_true_on_missing_file() -> None:
    assert not config_store.config_path().exists()
    assert config_store.load_mt5_sounds_muted() is True


def test_load_mt5_sounds_muted_defaults_true_on_corrupt_file() -> None:
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json at all", encoding="utf-8")

    assert config_store.load_mt5_sounds_muted() is True


def test_load_mt5_sounds_muted_defaults_true_on_legacy_two_key_file() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")

    assert config_store.load_mt5_sounds_muted() is True
    assert config_store.load_mt5_mute_pending() is False


@pytest.mark.parametrize("bad_value", ["no", 0, None])
def test_load_mt5_sounds_muted_defaults_true_for_non_bool_stored_value(bad_value: object) -> None:
    path = config_store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mt5_sounds_muted": bad_value}), encoding="utf-8")

    assert config_store.load_mt5_sounds_muted() is True


def test_save_mt5_sounds_muted_round_trips_false() -> None:
    config_store.save_mt5_sounds_muted(False)
    assert config_store.load_mt5_sounds_muted() is False


def test_load_mt5_mute_pending_defaults_false_and_round_trips_true() -> None:
    assert config_store.load_mt5_mute_pending() is False
    config_store.save_mt5_mute_pending(True)
    assert config_store.load_mt5_mute_pending() is True


def test_saving_sound_flags_preserves_ciphertext_token_and_base_url() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")
    config_store.save_mt5_sounds_muted(False)
    config_store.save_mt5_mute_pending(True)

    assert config_store.load_token() == "tok_abc123"
    assert config_store.load_base_url() == "https://treedger.com"


def test_clear_token_preserves_both_new_sound_keys() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_mt5_sounds_muted(False)
    config_store.save_mt5_mute_pending(True)

    config_store.clear_token()

    assert config_store.load_token() is None
    assert config_store.load_mt5_sounds_muted() is False
    assert config_store.load_mt5_mute_pending() is True


def test_config_file_holds_exactly_the_allow_list_after_all_four_keys_saved() -> None:
    config_store.save_token("tok_abc123")
    config_store.save_base_url("https://treedger.com")
    config_store.save_mt5_sounds_muted(False)
    config_store.save_mt5_mute_pending(True)

    with config_store.config_path().open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert set(data.keys()) == set(config_store.ALLOWED_KEYS)
    assert "account" not in data
    assert "login" not in data
    assert "server" not in data
    assert "password" not in data
    assert "investorPassword" not in data
