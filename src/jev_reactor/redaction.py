"""Redaction and size caps. Runs before every provider call and before every write.

Redaction is best effort, not a guarantee: it masks values under sensitive keys and
well-known credential shapes inside strings. It cannot know what *your* data considers
secret, so keep secrets out of state in the first place (allowlist the fields you send).
Reports contain key paths and counts, never values.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from jev_reactor.config import RedactionConfig

# token *counts* are not secrets
_NOT_SECRET_KEY = re.compile(
    r"(?:^|_)(?:input|output|total|max|prompt|completion|cached)_?tokens?$|token_(?:count|usage|limit)s?$"
)

_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
)
_KEY_VALUE = re.compile(
    r"""(?ix)\b(api[_-]?key|secret|token|password|passwd|pwd|authorization)\s*[=:]\s*(['"]?)([^\s'",;]{6,})\2"""
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass
class RedactionReport:
    """What was changed. Holds paths and counts only, never the values themselves."""

    masked: int = 0
    truncated: int = 0
    paths: list[str] = field(default_factory=list)

    def note(self, path: str, *, masked: bool) -> None:
        if masked:
            self.masked += 1
        else:
            self.truncated += 1
        if len(self.paths) < 20:
            self.paths.append(path)

    @property
    def changed(self) -> bool:
        return bool(self.masked or self.truncated)


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


class Redactor:
    def __init__(
        self,
        config: RedactionConfig | None = None,
        *,
        known_secrets: Iterable[str | None] = (),
        include_env_key: bool = True,
    ) -> None:
        self.config = config or RedactionConfig()
        secrets = {s for s in known_secrets if s and len(s) >= 6}
        if include_env_key:
            env_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
            if len(env_key) >= 6:
                secrets.add(env_key)
        self._known = sorted(secrets, key=len, reverse=True)
        self._sensitive = tuple(_normalise_key(k) for k in self.config.sensitive_keys)

    def add_secret(self, value: str) -> None:
        if len(value) >= 6 and value not in self._known:
            self._known.insert(0, value)

    # ------------------------------------------------------------------ public

    def redact(self, value: Any) -> tuple[Any, RedactionReport]:
        report = RedactionReport()
        return self._walk(value, "$", 0, report), report

    def redact_text(self, text: str) -> str:
        return self._scrub_string(text, "$", RedactionReport())

    # ------------------------------------------------------------------ internals

    def _is_sensitive_key(self, key: str) -> bool:
        norm = _normalise_key(key)
        if _NOT_SECRET_KEY.search(norm):
            return False
        return any(s and s in norm for s in self._sensitive)

    def _walk(self, value: Any, path: str, depth: int, report: RedactionReport) -> Any:
        cfg = self.config
        if depth > cfg.max_depth:
            report.note(path, masked=False)
            return "[MAX_DEPTH]"
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self._scrub_string(value, path, report)
        if isinstance(value, bytes | bytearray):
            report.note(path, masked=True)
            return f"[bytes:{len(value)}]"
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                child = f"{path}.{key}"
                if self._is_sensitive_key(key) and v is not None and not isinstance(v, bool):
                    report.note(child, masked=True)
                    out[key] = cfg.replacement
                else:
                    out[key] = self._walk(v, child, depth + 1, report)
            return out
        if isinstance(value, list | tuple | set | frozenset):
            items = sorted(value, key=repr) if isinstance(value, set | frozenset) else list(value)
            kept = [
                self._walk(v, f"{path}[{i}]", depth + 1, report)
                for i, v in enumerate(items[: cfg.max_list_items])
            ]
            if len(items) > cfg.max_list_items:
                report.note(path, masked=False)
                kept.append(f"[+{len(items) - cfg.max_list_items} more items]")
            return kept
        # unknown object: fall back to a bounded string form
        return self._scrub_string(str(value), path, report)

    def _scrub_string(self, text: str, path: str, report: RedactionReport) -> str:
        cfg = self.config
        original = text
        for secret in self._known:
            if secret in text:
                text = text.replace(secret, cfg.replacement)
        for _name, pattern in _VALUE_PATTERNS:
            text = pattern.sub(cfg.replacement, text)
        text = _KEY_VALUE.sub(lambda m: f"{m.group(1)}={cfg.replacement}", text)
        if cfg.redact_emails:
            text = _EMAIL.sub(cfg.replacement, text)
        if text != original:
            report.note(path, masked=True)
        if len(text) > cfg.max_string_chars:
            extra = len(text) - cfg.max_string_chars
            text = f"{text[: cfg.max_string_chars]}…[truncated {extra} chars]"
            report.note(path, masked=False)
        return text
