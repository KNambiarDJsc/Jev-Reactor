from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jev_reactor.config import RedactionConfig
from jev_reactor.redaction import Redactor

FAKE_SECRETS = {
    "openai": "sk-abcdefghijklmnopqrstuvwx1234",
    "anthropic": "sk-ant-abcdefghijklmnopqrstuvwx",
    "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "aws": "AKIAABCDEFGHIJKLMNOP",
    "slack": "xoxb-1234567890-abcdefghij",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcDEF123456",
    "google": "AIzaSyA-abcdefghijklmnopqrstuvwxyz012345",
}


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(include_env_key=False)


def test_values_under_sensitive_keys_are_masked_at_any_depth(redactor: Redactor) -> None:
    state = {
        "goal": "find invoice",
        "auth": {"Authorization": "Bearer abc123def456", "x-api-key": "whatever-value"},
        "creds": [{"password": "hunter2hunter2"}, {"note": "fine"}],
    }
    out, report = redactor.redact(state)
    assert out["auth"]["Authorization"] == "[REDACTED]"
    assert out["auth"]["x-api-key"] == "[REDACTED]"
    assert out["creds"][0]["password"] == "[REDACTED]"
    assert out["creds"][1]["note"] == "fine"
    assert out["goal"] == "find invoice"
    assert report.masked == 3


@pytest.mark.parametrize("secret", FAKE_SECRETS.values(), ids=FAKE_SECRETS.keys())
def test_well_known_credential_shapes_are_masked_inside_strings(
    redactor: Redactor, secret: str
) -> None:
    out, report = redactor.redact({"note": f"please use {secret} for the call"})
    assert secret not in json.dumps(out)
    assert report.masked == 1


def test_pem_private_key_blocks_are_masked(redactor: Redactor) -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    out, _ = redactor.redact({"file": f"contents:\n{pem}\nend"})
    assert "MIIEow" not in json.dumps(out)


def test_url_credentials_and_key_value_pairs_are_masked(redactor: Redactor) -> None:
    out, _ = redactor.redact(
        {"a": "postgres://admin:s3cretpass@db.internal/app", "b": "password=hunter2hunter2 ok"}
    )
    assert "s3cretpass" not in json.dumps(out)
    assert "hunter2hunter2" not in json.dumps(out)
    assert "db.internal" in out["a"]


def test_token_counts_are_not_secrets(redactor: Redactor) -> None:
    out, report = redactor.redact({"usage": {"input_tokens": 332, "max_tokens": 100}})
    assert out["usage"] == {"input_tokens": 332, "max_tokens": 100}
    assert not report.changed


def test_the_configured_api_key_is_scrubbed_wherever_it_appears() -> None:
    redactor = Redactor(known_secrets=["tsk_live_9f8e7d6c5b4a"], include_env_key=False)
    out, _ = redactor.redact({"log": "called with tsk_live_9f8e7d6c5b4a ok"})
    assert "tsk_live_9f8e7d6c5b4a" not in json.dumps(out)


def test_api_key_from_the_environment_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key-value-123456")
    out, _ = Redactor().redact({"x": "the key is env-key-value-123456."})
    assert "env-key-value-123456" not in json.dumps(out)


def test_emails_are_kept_unless_asked(redactor: Redactor) -> None:
    out, _ = redactor.redact({"who": "mail a@example.com"})
    assert "a@example.com" in out["who"]
    strict = Redactor(RedactionConfig(redact_emails=True), include_env_key=False)
    out, _ = strict.redact({"who": "mail a@example.com"})
    assert "a@example.com" not in out["who"]


def test_size_caps_truncate_strings_lists_and_depth() -> None:
    redactor = Redactor(
        RedactionConfig(max_string_chars=32, max_list_items=3, max_depth=2), include_env_key=False
    )
    out, report = redactor.redact(
        {"s": "x" * 100, "l": list(range(10)), "d": {"a": {"b": {"c": 1}}}}
    )
    assert out["s"].startswith("x" * 32) and "truncated 68 chars" in out["s"]
    assert out["l"][:3] == [0, 1, 2] and out["l"][3] == "[+7 more items]"
    assert out["d"]["a"]["b"] == "[MAX_DEPTH]"
    assert report.truncated == 3


def test_report_never_contains_values(redactor: Redactor) -> None:
    _, report = redactor.redact({"password": "topsecretvalue"})
    assert "topsecretvalue" not in repr(report)
    assert report.paths == ["$.password"]


def test_input_is_not_mutated_and_redaction_is_idempotent(redactor: Redactor) -> None:
    state = {"token": "abcdef123456", "items": [1, 2, {"api_key": "abcdef123456"}]}
    snapshot = json.dumps(state)
    once, _ = redactor.redact(state)
    twice, _ = redactor.redact(once)
    assert json.dumps(state) == snapshot
    assert once == twice


def test_non_json_values_are_made_serialisable(redactor: Redactor) -> None:
    from datetime import UTC, datetime

    out, _ = redactor.redact(
        {"when": datetime(2026, 1, 2, tzinfo=UTC), "raw": b"\x00\x01", "s": {3, 1}}
    )
    json.dumps(out)
    assert out["when"].startswith("2026-01-02")


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(st.text(max_size=40), st.integers(), st.lists(st.text(max_size=10), max_size=5)),
        max_size=6,
    )
)
def test_property_output_is_always_json_and_never_larger_than_the_caps(state: dict) -> None:  # type: ignore[type-arg]
    redactor = Redactor(
        RedactionConfig(max_string_chars=20, max_list_items=3), include_env_key=False
    )
    out, _ = redactor.redact(state)
    json.dumps(out)
    for value in out.values():
        if isinstance(value, str):
            assert len(value) <= 20 + len("…[truncated 99999 chars]")
        if isinstance(value, list):
            assert len(value) <= 4
