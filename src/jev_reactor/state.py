"""Compact, bounded state.

Jev's docs are blunt about this: accuracy falls as unrelated detail is added ("context
rot"), and a large state makes it harder to see what produced a wrong answer. The state
builder's job is to send *only* what the questions need, redacted and size-capped.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Sequence
from typing import Any

from jev_reactor.errors import ReactorError
from jev_reactor.models import ReactorEvent, canonical_json


class StateTooLargeError(ReactorError):
    """State exceeds the configured cap even after trimming history."""

    def __init__(self, chars: int, limit: int) -> None:
        self.chars = chars
        self.limit = limit
        super().__init__(f"state is {chars} characters, over the {limit} character limit")


def state_chars(state: Any) -> int:
    return len(canonical_json(state))


def default_state(event: ReactorEvent) -> dict[str, Any]:
    """Event state plus the goal and the proposed action, without overriding host keys."""
    state = dict(event.state)
    if event.goal is not None:
        state.setdefault("goal", event.goal)
    if event.proposed_action is not None:
        state.setdefault("proposed_action", event.proposed_action)
    return state


_TOKEN = re.compile(r"([A-Za-z_][\w-]*)|\[(\d+)\]")


def _tokens(path: str) -> list[str | int]:
    out: list[str | int] = []
    for name, index in _TOKEN.findall(path):
        out.append(int(index) if index else name)
    return out


def get_path(obj: Any, path: str) -> Any:
    """Resolve ``a.b[0].c`` against nested dicts/lists. Raises ``KeyError`` if absent."""
    cur = obj
    for token in _tokens(path):
        try:
            cur = cur[token]
        except (KeyError, IndexError, TypeError) as exc:
            raise KeyError(path) from exc
    return cur


def normalise_path(path: str) -> str:
    """``recent_calls[3].tool`` -> ``recent_calls[].tool`` so indexes compare as wildcards."""
    return re.sub(r"\[\d+\]", "[]", path)


def path_declared(reference: str, declared: Iterable[str]) -> bool:
    """A reference resolves if it is, sits under, or is an ancestor of a declared path."""
    ref = normalise_path(reference)
    for d in declared:
        dec = normalise_path(d)
        if (
            ref == dec
            or ref.startswith((dec + ".", dec + "["))
            or dec.startswith((ref + ".", ref + "["))
        ):
            return True
    return False


def allowlist_state(state: dict[str, Any], paths: Sequence[str]) -> dict[str, Any]:
    """Keep only the listed dotted paths (no indexes). An empty allowlist keeps nothing."""
    out: dict[str, Any] = {}
    for path in paths:
        keys = path.split(".")
        src: Any = state
        for key in keys:
            if isinstance(src, dict) and key in src:
                src = src[key]
            else:
                break
        else:
            dst = out
            for key in keys[:-1]:
                dst = dst.setdefault(key, {})
            dst[keys[-1]] = copy.deepcopy(src)
    return out


def bound_state(
    state: dict[str, Any], *, max_chars: int, trim_lists: Sequence[str] = ()
) -> dict[str, Any]:
    """Fit ``state`` under ``max_chars`` by dropping the *oldest* items of named lists.

    Lists are assumed ordered oldest to newest, as in a trace. Nothing is summarised: Jev
    does not generate text, and a summary silently loses the exact path or error that a
    later question may need. If trimming is not enough, raise ``StateTooLargeError`` and
    let the policy fall back instead of sending a state that is known to be too big.
    """
    if state_chars(state) <= max_chars:
        return state
    trimmed = copy.deepcopy(state)
    for key in trim_lists:
        items = trimmed.get(key)
        while isinstance(items, list) and items and state_chars(trimmed) > max_chars:
            items.pop(0)
    size = state_chars(trimmed)
    if size > max_chars:
        raise StateTooLargeError(size, max_chars)
    return trimmed
