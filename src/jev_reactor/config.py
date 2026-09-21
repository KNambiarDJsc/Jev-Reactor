"""Configuration. Defaults are conservative: unavailable means *review*, never *allow*."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from jev_reactor.errors import ConfigError
from jev_reactor.models import FailureMode

DEFAULT_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_TIMEOUT_SECONDS = 1.0

#: key names whose values are always masked before a provider call or persistence
DEFAULT_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "token",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "privatekey",
    "bearer",
    "access_key",
)


class TypeSafeSettings(BaseModel):
    """Settings for the TypeSafe Jev provider, read from the environment."""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, repr=False)
    #: ``jev-latest`` is an alias that moves when a new release ships. Pin a versioned id
    #: (for example ``jev-1.13.0``) once you have tuned thresholds against it.
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    #: SDK retries allowed *inside* the deadline. The SDK default budget is 30 s, far more
    #: than an interactive decision loop can spend, so the adapter bounds it explicitly.
    max_retries: int = Field(default=0, ge=0, le=5)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TypeSafeSettings:
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        raw_timeout = get("JEV_TIMEOUT_SECONDS")
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        except ValueError as exc:
            raise ConfigError(f"JEV_TIMEOUT_SECONDS must be a number, got {raw_timeout!r}") from exc
        key = get("TYPESAFE_API_KEY")
        return cls(
            api_key=SecretStr(key) if key else None,
            model=get("TYPESAFE_DEFAULT_MODEL") or DEFAULT_MODEL,
            base_url=get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL,
            timeout_seconds=timeout,
        )


class BreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: consecutive transient failures before the circuit opens
    failure_threshold: int = Field(default=5, ge=1)
    #: how long the circuit stays open before one probe call is allowed
    recovery_seconds: float = Field(default=10.0, ge=0)


class RedactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sensitive_keys: tuple[str, ...] = DEFAULT_SENSITIVE_KEYS
    replacement: str = "[REDACTED]"
    #: emails are personal data rather than secrets, so masking them is opt-in
    redact_emails: bool = False
    max_string_chars: int = Field(default=2000, ge=16)
    max_list_items: int = Field(default=50, ge=1)
    max_depth: int = Field(default=8, ge=1)


#: what a host should do when the decision cannot be made, by tool risk tier
DEFAULT_FAILURE_BY_RISK: dict[str, FailureMode] = {
    "read": "fallback",
    "write": "review",
    "irreversible": "block",
}


class ReactorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: per-decision deadline; also handed to the provider as its timeout
    deadline_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    #: upper bound on concurrent provider calls *and* on queued decisions in ``run()``
    max_in_flight: int = Field(default=8, ge=1)
    #: ``cancel``: a newer event for the same stream supersedes older in-flight decisions
    stale: Literal["cancel", "keep"] = "cancel"
    #: used when the provider is unavailable and the policy has no opinion
    on_provider_failure: FailureMode = "review"
    failure_by_risk: dict[str, FailureMode] = Field(
        default_factory=lambda: dict(DEFAULT_FAILURE_BY_RISK)
    )
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    #: if set, only these dotted state paths are sent to the provider (a field allowlist)
    state_allowlist: list[str] | None = None
    #: refuse to send more than this many characters of state; oversized state is a
    #: documented accuracy hazard ("context rot"), not just a limit
    max_state_chars: int = Field(default=24_000, ge=256)
    #: ``digest`` stores only a hash of the redacted state; ``redacted`` stores the state
    persist_state: Literal["digest", "redacted"] = "digest"
    #: raw provider payloads may contain sensitive content, so this is explicit opt-in
    persist_raw_response: bool = False
    #: keep a provider call alive this long past the deadline so a *late* result can be
    #: recorded for tuning. It is never applied to the decision that already went out.
    late_grace_seconds: float = Field(default=0.0, ge=0)
    #: raise instead of degrading to ``review`` when a policy misbehaves (use in tests/CI)
    strict_policy_errors: bool = False

    @field_validator("failure_by_risk")
    @classmethod
    def _irreversible_never_allows(cls, value: dict[str, FailureMode]) -> dict[str, FailureMode]:
        if value.get("irreversible") == "allow":
            raise ValueError("an unavailable provider must never auto-allow an irreversible action")
        return value
