"""Built-in question packs and their policies."""

from __future__ import annotations

from collections.abc import Callable

from jev_reactor.packs.agent_loop import AgentLoopPolicy, AgentLoopThresholds, agent_loop_pack
from jev_reactor.packs.context import (
    CompactionPlan,
    ContextPolicy,
    ContextThresholds,
    classify_items,
    context_pack,
)
from jev_reactor.packs.tool_loop import (
    PRESETS,
    ToolLoop,
    ToolLoopPolicy,
    ToolLoopThresholds,
    ToolSpec,
    tool_loop_pack,
)
from jev_reactor.questions import QuestionPack

#: name -> factory, used by ``jev-reactor export-pack`` and the drift test
BUILTIN_PACKS: dict[str, Callable[[], QuestionPack]] = {
    "tool-loop": tool_loop_pack,
    "context-retention": context_pack,
    "agent-loop": agent_loop_pack,
}

__all__ = [
    "BUILTIN_PACKS",
    "PRESETS",
    "AgentLoopPolicy",
    "AgentLoopThresholds",
    "CompactionPlan",
    "ContextPolicy",
    "ContextThresholds",
    "ToolLoop",
    "ToolLoopPolicy",
    "ToolLoopThresholds",
    "ToolSpec",
    "agent_loop_pack",
    "classify_items",
    "context_pack",
    "tool_loop_pack",
]
