"""The Reactor: event -> bounded state -> parallel typed questions -> policy -> action.

The Reactor never executes anything. It returns an immutable :class:`ActionDecision`; the
host application owns every side effect.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
import uuid
from collections import Counter
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from jev_reactor.breaker import CircuitBreaker
from jev_reactor.config import ReactorConfig
from jev_reactor.errors import (
    ConfigError,
    InvalidAnswerError,
    PolicyError,
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
    ReactorError,
)
from jev_reactor.models import (
    ActionDecision,
    DecisionEvent,
    DecisionResponse,
    ProviderStatus,
    ReactorEvent,
    sha256_hex,
)
from jev_reactor.policy import Policy, decision, failure_decision
from jev_reactor.providers.base import DecisionProvider
from jev_reactor.questions import QuestionPack, validate_response
from jev_reactor.redaction import RedactionReport, Redactor
from jev_reactor.sinks import Sink
from jev_reactor.state import StateTooLargeError, allowlist_state, bound_state

logger = logging.getLogger("jev_reactor")

StreamKey = Callable[[ReactorEvent], str]


def default_stream_key(event: ReactorEvent) -> str:
    """Events sharing a key supersede each other. Set ``metadata["stream_id"]`` per session."""
    meta = event.metadata
    return str(meta.get("stream_id") or meta.get("session_id") or "default")


@dataclass
class _Outcome:
    status: ProviderStatus
    response: DecisionResponse | None = None
    error: ProviderError | None = None
    late_task: asyncio.Future[DecisionResponse] | None = None


class _SourceFailure:
    """Carries an exception raised by the event source through the ordered queue."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class Reactor:
    def __init__(
        self,
        provider: DecisionProvider,
        *,
        pack: QuestionPack | None = None,
        policy: Policy | None = None,
        config: ReactorConfig | None = None,
        sinks: Sequence[Sink] = (),
        redactor: Redactor | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.pack = pack
        self.policy = policy
        self.config = config or ReactorConfig()
        self.sinks = list(sinks)
        self.redactor = redactor or Redactor(self.config.redaction)
        self.breaker = breaker or CircuitBreaker(self.config.breaker, clock=clock)
        self._clock = clock
        self._sem = asyncio.Semaphore(self.config.max_in_flight)
        self._seq = itertools.count(1)
        self._background: set[asyncio.Task[Any]] = set()
        self._resolved_model: str | None = None
        #: counters: provider_calls plus one per provider_status
        self.stats: Counter[str] = Counter()

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        for sink in self.sinks:
            closer = getattr(sink, "close", None)
            if callable(closer):
                closer()

    async def __aenter__(self) -> Reactor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ events

    def new_event(
        self,
        event_type: str,
        state: Mapping[str, Any] | None = None,
        *,
        goal: str | None = None,
        proposed_action: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ReactorEvent:
        """Create an event with a fresh id, the next sequence number, and a UTC timestamp."""
        return ReactorEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            sequence=next(self._seq),
            timestamp=datetime.now(UTC),
            event_type=event_type,
            state=dict(state or {}),
            goal=goal,
            proposed_action=dict(proposed_action) if proposed_action is not None else None,
            metadata=dict(metadata or {}),
        )

    def _coerce_event(self, raw: ReactorEvent | Mapping[str, Any]) -> ReactorEvent:
        if isinstance(raw, ReactorEvent):
            return raw
        data = dict(raw)
        data.setdefault("event_id", f"evt_{uuid.uuid4().hex[:12]}")
        data.setdefault("sequence", next(self._seq))
        data.setdefault("timestamp", datetime.now(UTC))
        data.setdefault("event_type", "event")
        data.setdefault("state", {})
        return ReactorEvent.model_validate(data)

    # ------------------------------------------------------------------ one-shot API

    async def decide(
        self,
        event: ReactorEvent | Mapping[str, Any],
        question_pack: QuestionPack | None = None,
        policy: Policy | None = None,
    ) -> ActionDecision:
        """Evaluate one event and return an immutable decision. Nothing is executed."""
        return (await self.decide_record(event, question_pack, policy)).policy_result

    async def decide_record(
        self,
        event: ReactorEvent | Mapping[str, Any],
        question_pack: QuestionPack | None = None,
        policy: Policy | None = None,
        *,
        superseded: asyncio.Event | None = None,
    ) -> DecisionEvent:
        """Like :meth:`decide` but returns the full recorded :class:`DecisionEvent`."""
        pack = question_pack or self.pack
        pol = policy or self.policy
        if pack is None:
            raise ConfigError("no question pack: pass question_pack= or set Reactor(pack=...)")
        if pol is None:
            raise ConfigError("no policy: pass policy= or set Reactor(policy=...)")
        ev = self._coerce_event(event)
        started = self._clock()

        state: dict[str, Any] = {}
        report = RedactionReport()
        response: DecisionResponse | None = None
        late_task: asyncio.Future[DecisionResponse] | None = None

        # 1. hard rules first: they depend only on the event, never on Jev or on the state
        #    builder, so a malformed or forbidden proposal is rejected deterministically.
        status: ProviderStatus
        hard = self._pre_check(pol, ev)
        if hard is not None:
            result, status = hard, "not_called"
        else:
            # 2. compact, redacted, bounded state --------------------------------------------
            try:
                state, report = self._build_state(ev, pack)
            except StateTooLargeError:
                result = self._failure(pol, ev, "not_called", "state_too_large")
                status = "not_called"
            except Exception as exc:
                if self.config.strict_policy_errors:
                    raise
                logger.warning("state builder for pack %s raised %s", pack.id, type(exc).__name__)
                result = self._failure(pol, ev, "not_called", "state_build_error")
                status = "not_called"
            else:
                if not self.breaker.allow():
                    result, status = self._failure(pol, ev, "circuit_open"), "circuit_open"
                else:
                    # 3. one request, every question, under a deadline ------------------------
                    outcome = await self._call_provider(state, pack, ev, superseded, started)
                    status, response, late_task = (
                        outcome.status,
                        outcome.response,
                        outcome.late_task,
                    )
                    if status == "ok" and response is not None:
                        try:
                            validate_response(response, pack.questions)
                        except InvalidAnswerError as exc:
                            outcome.error = ProviderResponseError(str(exc))
                            self.breaker.record_failure(outcome.error)
                            status, response = "error", None
                        else:
                            self.breaker.record_success()
                            self._note_model(response.model)
                    # 4. deterministic policy -------------------------------------------------
                    if status == "ok" and response is not None:
                        result = self._apply_policy(pol, ev, response)
                    elif status == "stale":
                        result = decision("fallback", "superseded", target="newer_event")
                    else:
                        extra = (outcome.error.kind,) if outcome.error and status == "error" else ()
                        result = self._failure(pol, ev, status, *extra)

        record = self._record(ev, pack, pol, state, report, response, result, status, started)
        self.stats[f"status_{status}"] += 1
        await self._emit(record)
        if late_task is not None:
            self._spawn(self._observe_late(late_task, record, pack, pol, ev))
        return record

    # ------------------------------------------------------------------ streaming API

    async def run(
        self,
        events: AsyncIterable[ReactorEvent | Mapping[str, Any]]
        | Iterable[ReactorEvent | Mapping[str, Any]],
        question_pack: QuestionPack | None = None,
        policy: Policy | None = None,
        *,
        stream_key: StreamKey = default_stream_key,
    ) -> AsyncGenerator[ActionDecision, None]:
        """Decide a stream of events. Results come back in sequence order.

        Backpressure and bounds: a queue of at most ``max_in_flight`` decisions sits between
        the event source and the consumer, so a slow consumer slows the source instead of
        growing memory, and no more than ``max_in_flight`` provider calls run at once.
        With ``stale="cancel"`` a newer event on the same stream supersedes older in-flight
        decisions: their responses are discarded and they come back as ``fallback`` /
        ``superseded``, so an old response can never drive a newer situation.
        """
        inner = self.run_records(events, question_pack, policy, stream_key=stream_key)
        async with contextlib.aclosing(inner):
            async for record in inner:
                yield record.policy_result

    async def run_records(
        self,
        events: AsyncIterable[ReactorEvent | Mapping[str, Any]]
        | Iterable[ReactorEvent | Mapping[str, Any]],
        question_pack: QuestionPack | None = None,
        policy: Policy | None = None,
        *,
        stream_key: StreamKey = default_stream_key,
    ) -> AsyncGenerator[DecisionEvent, None]:
        queue: asyncio.Queue[asyncio.Task[DecisionEvent] | _SourceFailure | None] = asyncio.Queue(
            maxsize=self.config.max_in_flight
        )
        active: dict[str, list[tuple[int, asyncio.Event]]] = {}
        tasks: set[asyncio.Task[DecisionEvent]] = set()

        def forget(key: str, entry: tuple[int, asyncio.Event]) -> None:
            entries = active.get(key)
            if entries and entry in entries:
                entries.remove(entry)

        def cleanup_for(
            key: str, entry: tuple[int, asyncio.Event]
        ) -> Callable[[asyncio.Task[DecisionEvent]], None]:
            def cleanup(task: asyncio.Task[DecisionEvent]) -> None:
                tasks.discard(task)
                forget(key, entry)

            return cleanup

        async def feeder() -> None:
            try:
                async with contextlib.aclosing(_aiter(events)) as source:
                    async for raw in source:
                        ev = self._coerce_event(raw)
                        key = stream_key(ev)
                        if self.config.stale == "cancel":
                            for old_seq, old_flag in active.get(key, ()):
                                if old_seq < ev.sequence:
                                    old_flag.set()
                        flag = asyncio.Event()
                        entry = (ev.sequence, flag)
                        active.setdefault(key, []).append(entry)
                        task = asyncio.create_task(
                            self.decide_record(ev, question_pack, policy, superseded=flag)
                        )
                        tasks.add(task)
                        task.add_done_callback(cleanup_for(key, entry))
                        await queue.put(task)  # blocks when the consumer is slow: backpressure
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(_SourceFailure(exc))
                return
            await queue.put(None)

        feed = asyncio.create_task(feeder())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, _SourceFailure):
                    raise item.exc
                yield await item
        finally:
            feed.cancel()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(feed, *tasks, return_exceptions=True)

    # ------------------------------------------------------------------ stages

    def _build_state(
        self, ev: ReactorEvent, pack: QuestionPack
    ) -> tuple[dict[str, Any], RedactionReport]:
        return build_provider_state(ev, pack, self.config, self.redactor)

    def preview_state(
        self, event: ReactorEvent | Mapping[str, Any], question_pack: QuestionPack | None = None
    ) -> dict[str, Any]:
        """The exact, redacted, bounded state the provider would receive for ``event``."""
        pack = question_pack or self.pack
        if pack is None:
            raise ConfigError("no question pack: pass question_pack= or set Reactor(pack=...)")
        return self._build_state(self._coerce_event(event), pack)[0]

    def _pre_check(self, pol: Policy, ev: ReactorEvent) -> ActionDecision | None:
        hook = getattr(pol, "pre_check", None)
        if hook is None:
            return None
        try:
            result: ActionDecision | None = hook(ev)
        except Exception as exc:
            if self.config.strict_policy_errors:
                raise
            logger.warning("pre_check of %s raised %s", _policy_id(pol), type(exc).__name__)
            return decision(
                "review", "policy_error", target="operator", error_type=type(exc).__name__
            )
        return result

    async def _call_provider(
        self,
        state: dict[str, Any],
        pack: QuestionPack,
        ev: ReactorEvent,
        superseded: asyncio.Event | None,
        started: float,
    ) -> _Outcome:
        deadline = self.config.deadline_seconds
        remaining = deadline - (self._clock() - started)
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=max(remaining, 0.0))
        except TimeoutError:
            self.breaker.release_probe()  # never reached the provider
            return _Outcome(
                "timeout", error=ProviderTimeoutError("no free provider slot before the deadline")
            )

        remaining = max(deadline - (self._clock() - started), 1e-3)
        self.stats["provider_calls"] += 1
        call: asyncio.Future[DecisionResponse] = asyncio.ensure_future(
            self.provider.evaluate(state=state, questions=pack.questions, timeout=remaining)
        )
        call.add_done_callback(lambda _f: self._sem.release())
        waiters: set[asyncio.Future[Any]] = {call}
        stale_wait: asyncio.Future[Any] | None = None
        if superseded is not None:
            stale_wait = asyncio.ensure_future(superseded.wait())
            waiters.add(stale_wait)
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            call.cancel()
            self.breaker.release_probe()
            raise
        finally:
            if stale_wait is not None and not stale_wait.done():
                stale_wait.cancel()

        if superseded is not None and superseded.is_set():
            # a newer event exists: whatever this call returns must not be applied
            call.cancel()
            self.breaker.release_probe()
            return _Outcome("stale")
        if call in done:
            try:
                return _Outcome("ok", response=call.result())
            except ProviderTimeoutError as exc:
                self.breaker.record_failure(exc)
                return _Outcome("timeout", error=exc)
            except ProviderError as exc:
                self.breaker.record_failure(exc)
                return _Outcome("error", error=exc)
            except (asyncio.CancelledError, ConfigError):
                raise
            except Exception as exc:
                err = ProviderError(f"unexpected provider failure: {type(exc).__name__}")
                self.breaker.record_failure(err)
                return _Outcome("error", error=err)

        # deadline passed with no answer
        timeout_err = ProviderTimeoutError(f"no response within {deadline:.3f}s")
        self.breaker.record_failure(timeout_err)
        if self.config.late_grace_seconds > 0:
            return _Outcome("timeout", error=timeout_err, late_task=call)
        call.cancel()
        return _Outcome("timeout", error=timeout_err)

    def _apply_policy(
        self, pol: Policy, ev: ReactorEvent, response: DecisionResponse
    ) -> ActionDecision:
        try:
            out: Any = pol.decide(event=ev, response=response)
            if isinstance(out, Mapping):
                out = ActionDecision.model_validate(out)
            if not isinstance(out, ActionDecision):
                raise PolicyError(f"policy returned {type(out).__name__}, not an ActionDecision")
            return out
        except (ValidationError, PolicyError) as exc:
            error = PolicyError(f"policy {_policy_id(pol)} returned an invalid decision: {exc}")
            if self.config.strict_policy_errors:
                raise error from exc
            logger.warning("policy %s returned an invalid decision", _policy_id(pol))
            return decision(
                "review", "policy_error", target="operator", error_type="invalid_decision"
            )
        except Exception as exc:
            if self.config.strict_policy_errors:
                raise
            logger.warning("policy %s raised %s", _policy_id(pol), type(exc).__name__)
            return decision(
                "review", "policy_error", target="operator", error_type=type(exc).__name__
            )

    def _failure(
        self, pol: Policy, ev: ReactorEvent, status: ProviderStatus, *reasons: str
    ) -> ActionDecision:
        """The configured fail-safe decision. Unavailable never silently becomes allow."""
        result: ActionDecision | None = None
        hook = getattr(pol, "on_unavailable", None)
        if hook is not None:
            try:
                result = hook(ev, status, self.config)
            except Exception:
                result = None
        if result is None:
            result = failure_decision(self.config.on_provider_failure, status)
        if reasons:
            result = result.model_copy(update={"reason_codes": [*result.reason_codes, *reasons]})
        return result

    def _record(
        self,
        ev: ReactorEvent,
        pack: QuestionPack,
        pol: Policy,
        state: dict[str, Any],
        report: RedactionReport,
        response: DecisionResponse | None,
        result: ActionDecision,
        status: ProviderStatus,
        started: float,
    ) -> DecisionEvent:
        cfg = self.config
        keep_state = cfg.persist_state == "redacted"
        stored_response = response
        if response is not None and not cfg.persist_raw_response:
            stored_response = response.model_copy(update={"raw": None})
        meta, _ = self.redactor.redact(ev.metadata)
        action, _ = self.redactor.redact(ev.proposed_action) if ev.proposed_action else (None, None)
        persisted = ev.model_copy(
            update={
                "state": state if keep_state else {},
                "metadata": meta,
                "proposed_action": action,
                "goal": self.redactor.redact_text(ev.goal) if (ev.goal and keep_state) else None,
            }
        )
        stamped = result.model_copy(
            update={"event_id": ev.event_id, "sequence": ev.sequence, "provider_status": status}
        )
        return DecisionEvent(
            event=persisted,
            question_pack_id=pack.id,
            question_pack_version=pack.version,
            question_pack_fingerprint=pack.fingerprint(),
            policy_id=_policy_id(pol),
            response=stored_response,
            policy_result=stamped,
            provider_status=status,
            decision_latency_ms=(self._clock() - started) * 1000.0,
            state_digest=sha256_hex(state, 24) if state else None,
            state_persisted=keep_state,
        )

    async def _emit(self, record: DecisionEvent) -> None:
        if not self.sinks:
            return
        results = await asyncio.gather(
            *(s.emit(record) for s in self.sinks), return_exceptions=True
        )
        for sink, res in zip(self.sinks, results, strict=True):
            if isinstance(res, BaseException):
                # never include the record: it may describe sensitive state
                logger.warning("sink %s failed: %s", type(sink).__name__, type(res).__name__)

    def _note_model(self, model: str) -> None:
        if self._resolved_model is not None and self._resolved_model != model:
            logger.warning(
                "resolved model changed from %s to %s; thresholds tuned on the old version may "
                "drift. Pin TYPESAFE_DEFAULT_MODEL to a versioned id to hold it steady.",
                self._resolved_model,
                model,
            )
        self._resolved_model = model

    # ------------------------------------------------------------------ late results

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _observe_late(
        self,
        call: asyncio.Future[DecisionResponse],
        record: DecisionEvent,
        pack: QuestionPack,
        pol: Policy,
        ev: ReactorEvent,
    ) -> None:
        """Record a result that arrived after the deadline. It is never applied.

        The record keeps the decision that was actually returned and adds, under
        ``outcome``, what the policy *would* have decided, which is what you need to tune
        the deadline.
        """
        started = self._clock()
        try:
            done, _ = await asyncio.wait({call}, timeout=self.config.late_grace_seconds)
            if call not in done:
                call.cancel()
                return
            response = call.result()
            validate_response(response, pack.questions)
            counterfactual = self._apply_policy(pol, ev, response)
        except asyncio.CancelledError:
            call.cancel()
            raise
        except Exception:
            return
        stored = response
        if not self.config.persist_raw_response:
            stored = response.model_copy(update={"raw": None})
        late = record.model_copy(
            update={
                "provider_status": "late",
                "response": stored,
                "outcome": {
                    "late_by_ms": round((self._clock() - started) * 1000.0, 1),
                    "late_counterfactual_action": counterfactual.action,
                    "late_counterfactual_reasons": counterfactual.reason_codes,
                },
            }
        )
        self.stats["status_late"] += 1
        await self._emit(late)


def build_provider_state(
    ev: ReactorEvent, pack: QuestionPack, config: ReactorConfig, redactor: Redactor
) -> tuple[dict[str, Any], RedactionReport]:
    """pack state builder -> optional field allowlist -> redaction -> size bound."""
    raw = pack.build_state(ev)
    if config.state_allowlist is not None:
        raw = allowlist_state(raw, config.state_allowlist)
    redacted, report = redactor.redact(raw)
    bounded = bound_state(redacted, max_chars=config.max_state_chars, trim_lists=pack.trim_lists)
    return bounded, report


def _policy_id(pol: Any) -> str:
    return str(getattr(pol, "policy_id", type(pol).__name__))


async def _aiter(source: Any) -> AsyncGenerator[Any, None]:
    if hasattr(source, "__aiter__"):
        iterator = source.__aiter__()
        try:
            while True:
                try:
                    item = await iterator.__anext__()
                except StopAsyncIteration:
                    return
                yield item
        finally:
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                await closer()
    else:
        for item in source:
            yield item
            await asyncio.sleep(0)  # let decisions and the consumer make progress


__all__ = ["Reactor", "ReactorError", "build_provider_state", "default_stream_key"]
