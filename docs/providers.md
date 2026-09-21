# Providers

A provider answers a batch of typed questions about a state. The core runtime depends on this
interface and nothing else, so the TypeSafe SDK is imported in exactly one module.

```python
class DecisionProvider(Protocol):
    async def evaluate(
        self, *, state: str | dict | list, questions: dict[str, QuestionSpec], timeout: float
    ) -> DecisionResponse: ...
```

A provider must respect `timeout` (seconds), raise this project's `ProviderError` subclasses
rather than SDK exceptions, put all questions in **one** request where it can, and never log
`state`.

## TypeSafe Jev (`TypeSafeProvider`)

```python
from jev_reactor import TypeSafeProvider  # imports the SDK lazily

provider = TypeSafeProvider()  # reads TYPESAFE_API_KEY etc. from the environment
```

| Setting | Env var | Default |
|---|---|---|
| API key | `TYPESAFE_API_KEY` | none; construction fails fast with a clear error |
| Model | `TYPESAFE_DEFAULT_MODEL` | `jev-latest` |
| Base URL | `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` |
| Timeout | `JEV_TIMEOUT_SECONDS` | `1.0` |

Written against `typesafe-sdk` 0.7.0 (`AsyncTypeSafeClient.system_one`, `Noul`/`Choice`/`Score`).
The contract tests run the **real SDK** against `httpx2.MockTransport`, so no network and no key
are involved.

### Pin the model once you have thresholds

`jev-latest` is an alias that "moves when a new release ships", so the answers behind it can
change without any change on your side. The response reports the resolved id (for example
`jev-1.13.0`); Reactor records it on every decision and warns when it changes mid-run,
and `jev-reactor report` warns about mixed versions. Once you have tuned thresholds, set
`TYPESAFE_DEFAULT_MODEL` to a versioned id and move to a new one on your own schedule.

### Deadlines and retries

The SDK's default retry policy allows up to 30 s across attempts (I measured a failing call
taking 1.22 s over three attempts with defaults), far more than an interactive decision can
spend. The adapter therefore always passes `RetryPolicy(max_retries=..., timeout=<deadline>)`
(default zero retries) and wraps the call in `asyncio.timeout`. The Reactor enforces the
deadline again on its own, so a provider that ignores `timeout` still cannot stall a decision.

### Error translation

| SDK exception | Project exception | Breaker |
|---|---|---|
| `TypeSafeAPITimeoutError` / `TimeoutError` | `ProviderTimeoutError` | counts |
| `TypeSafeAPIConnectionError`, `TypeSafeInternalServerError` (5xx, including 529 "overloaded") | `ProviderUnavailableError` | counts |
| `TypeSafeRateLimitError` (429) | `ProviderRateLimitedError` (`retry_after` in seconds) | counts; lengthens the open period |
| `TypeSafeAuthenticationError` / `TypeSafePermissionDeniedError` (401/403) | `ProviderAuthError` | opens immediately |
| `TypeSafeBadRequestError` / `NotFound` / `UnprocessableEntity` (400/404/422) | `ProviderRejectedError` | does not count (a pack bug) |
| `TypeSafeAPIResponseValidationError`, or an answer that fails validation | `ProviderResponseError` | does not count |

Error messages never include request bodies, so they never include your state.

### Things worth knowing

- **Request id.** The SDK's `response.request_id` raises when the `x-typesafe-request-id` header
  is absent (for example behind a gateway). The adapter treats it as optional.
- **Logging.** The SDK logs full request and response bodies, which contain your state, at
  DEBUG on the `typesafe_sdk` logger. Unless you configured that logger (or set
  `TYPESAFE_LOG_LEVEL`), the adapter pins it to INFO so a global DEBUG setting cannot leak
  state. It warns if you turn it on deliberately.
- **Raw payloads** are kept only with `TypeSafeProvider(include_raw=True)` *and*
  `persist_raw_response=True` on the Reactor.
- **Limits** stated in TypeSafe's Models page (as of 2026-09-21, and they say these change):
  64k tokens per request, 32k for state plus the longest question; 1,200 requests per minute.
  Keep state small anyway: the docs warn accuracy falls as unrelated detail grows.
- **Alternate endpoints.** `TYPESAFE_BASE_URL` and the SDK's `base_url`/`headers` exist for
  gateways. Third-party routes to Jev (Cloudflare, OpenRouter, Vercel AI Gateway) are described
  by the community; I have not tested them.

## `MockProvider`

Deterministic, no network, no key. It builds valid typed answers from compact descriptions:

```python
from jev_reactor import MockProvider

MockProvider({"relevant": 0.9, "next_action": ("call_tool", 0.8), "value": (1.4, 0.7)})
#            noul probability   choice + confidence            score + confidence
```

Choice probabilities are derived from the documented confidence formula, so mock answers are
internally consistent. It also supports first-match `rules`, a `respond` callable,
`MockProvider.from_fixture(path)`, simulated `latency`, scripted `raises=[...]` (`None` means
succeed), and `respect_timeout=False` to prove the Reactor's own deadline fires.

Answers left out on purpose are omitted from the response so the Reactor's validation notices.
Fixtures in this repository are hand-written: they report model `mock-fixture` and no latency.

## Writing your own provider

Any object with a matching `evaluate` works. A rule-based provider, for tests or for a fully
local system:

```python
# runnable
import asyncio

from jev_reactor import Answer, DecisionResponse, QuestionPack, QuestionSpec


class KeywordProvider:
    """Answers each Noul 0.95 if the state mentions a keyword, else 0.05."""

    def __init__(self, keyword: str) -> None:
        self.keyword = keyword

    async def evaluate(self, *, state, questions, timeout):
        hit = self.keyword in str(state).lower()
        answers = {
            qid: Answer(question_id=qid, type="noul", noul=0.95 if hit else 0.05)
            for qid, q in questions.items()
            if q.type == "noul"
        }
        return DecisionResponse(model="keyword-v1", answers=answers, latency_ms=0.0)


questions = {"urgent": QuestionSpec(id="urgent", type="noul", instructions="Is `msg` urgent?")}
response = asyncio.run(
    KeywordProvider("refund").evaluate(state={"msg": "Refund me"}, questions=questions, timeout=1.0)
)
print(response.answers["urgent"].noul)  # -> 0.95
```

### Local models, and a contract you should read

A provider backed by a local model is an interface, not a shipped feature: Reactor does not
include a fake local Jev. Several community projects reproduce the Choice/Score/Noul interface
on open models (poorjev, SemIf, Luce); I have not evaluated them.

**Do not train such a model on Jev's answers.** Reactor's recorded logs contain TypeSafe Output,
and section 2.3(b) of TypeSafe's Master Customer Agreement prohibits using Output "to perform
model distillation, train a model to imitate the output of the Services, or develop ... a
similar or competing product". Label your own data and train on that. This is a plain reading
of the agreement, not legal advice.

### Other vendors

TypeSafe publishes a System One Adapter that mirrors `system_one` over OpenAI- and
Anthropic-compatible APIs. Wrapping it (or any client) as a `DecisionProvider` is a few lines,
and is a natural fallback in a provider chain. Chains are on the roadmap, not in 0.1. The
provider interface is deliberately vendor-neutral: independent comparisons disagree about where
a small typed model wins, so measure on your own workload
(`jev-reactor report`, `jev-reactor replay`).

## Live tests

`tests/providers/test_typesafe_live.py` is skipped unless `TYPESAFE_LIVE_TESTS=1` and
`TYPESAFE_API_KEY` are both set (`pytest -m live`). It sends one benign request. It is not a
benchmark and not a security probe.
