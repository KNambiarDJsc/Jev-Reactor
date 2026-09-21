"""The public API. Everything a host application needs, and nothing it does not.

``TypeSafeProvider`` is deliberately not imported here: it is the only module that needs the
TypeSafe SDK, so it loads lazily (``from jev_reactor import TypeSafeProvider`` works).
"""

from __future__ import annotations

from jev_reactor.config import BreakerConfig, ReactorConfig, RedactionConfig, TypeSafeSettings
from jev_reactor.errors import (
    ConfigError,
    InvalidAnswerError,
    MissingApiKeyError,
    PackError,
    PolicyError,
    ProviderError,
    ReactorError,
    ReplayError,
)
from jev_reactor.metrics import Report, build_report
from jev_reactor.models import (
    ActionDecision,
    Answer,
    DecisionEvent,
    DecisionResponse,
    QuestionSpec,
    ReactorEvent,
)
from jev_reactor.packs import (
    AgentLoopPolicy,
    ContextPolicy,
    ToolLoop,
    ToolLoopPolicy,
    ToolLoopThresholds,
    ToolSpec,
    agent_loop_pack,
    classify_items,
    context_pack,
    tool_loop_pack,
)
from jev_reactor.policy import (
    Band,
    Policy,
    RuleChainPolicy,
    Signals,
    decision,
    noul_strength,
    weakest,
)
from jev_reactor.providers import DecisionProvider, MockProvider, auto_provider
from jev_reactor.questions import QuestionPack, lint_pack, load_pack, validate_response
from jev_reactor.reactor import Reactor
from jev_reactor.redaction import Redactor
from jev_reactor.replay import load_events, replay_events
from jev_reactor.sinks import JsonlSink, MemorySink, Sink, StdoutSink

__all__ = [
    "ActionDecision",
    "AgentLoopPolicy",
    "Answer",
    "Band",
    "BreakerConfig",
    "ConfigError",
    "ContextPolicy",
    "DecisionEvent",
    "DecisionProvider",
    "DecisionResponse",
    "InvalidAnswerError",
    "JsonlSink",
    "MemorySink",
    "MissingApiKeyError",
    "MockProvider",
    "PackError",
    "Policy",
    "PolicyError",
    "ProviderError",
    "QuestionPack",
    "QuestionSpec",
    "Reactor",
    "ReactorConfig",
    "ReactorError",
    "ReactorEvent",
    "RedactionConfig",
    "Redactor",
    "ReplayError",
    "Report",
    "RuleChainPolicy",
    "Signals",
    "Sink",
    "StdoutSink",
    "ToolLoop",
    "ToolLoopPolicy",
    "ToolLoopThresholds",
    "ToolSpec",
    "TypeSafeSettings",
    "agent_loop_pack",
    "auto_provider",
    "build_report",
    "classify_items",
    "context_pack",
    "decision",
    "lint_pack",
    "load_events",
    "load_pack",
    "noul_strength",
    "replay_events",
    "tool_loop_pack",
    "validate_response",
    "weakest",
]
