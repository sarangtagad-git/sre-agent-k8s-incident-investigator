"""The one place that talks to the OpenAI-compatible model provider (OpenRouter etc.).

Before this existed, two call sites each did `httpx.post(...)` then `raise_for_status()`.
raise_for_status() builds its message from the status line and URL only, so the server's
own explanation of a failure was thrown away. OpenRouter documents 403 as a permissions,
budget, guardrail or moderation problem and says which in the response body — and three of
thirteen attempts in the 2026-10-25 config C review died on a 403 whose cause we never
learned.
"""

from __future__ import annotations

import logging
import time

import httpx
from opentelemetry import trace

log = logging.getLogger(__name__)

_BODY_CAP = 1000  # characters of the provider's reply kept in the error
_RETRYABLE_STATUS = {408, 429}  # specific codes; every 5xx is covered by the >= 500 check

_MAX_ATTEMPTS = 3
_BACKOFF_START = 0.5  # seconds; doubles each retry: 0.5, 1.0, ...
_RETRY_AFTER_CAP = 30.0  # never trust a server asking for more than this
_DEADLINE = 120.0  # no wait that would end past this many seconds into the call


class ProviderError(Exception):
    """The provider's reply could not be used, with the reason it gave.

    `status` is the HTTP status (200 when the body itself was unusable), or None when
    nothing answered at all (timeout, connection failure). `retry_after` is the seconds the
    server asked us to wait (Retry-After header), when it sent one. The headers and the
    request are deliberately NOT kept: the API key travels in a header and the request
    holds cluster data (pod logs, events)."""

    def __init__(
        self,
        status: int | None,
        body: str = "",
        model: str = "",
        note: str = "",
        retry_after: float | None = None,  # last, so existing positional calls still work
    ):
        self.status = status
        self.body = body.strip()[:_BODY_CAP]
        self.model = model
        self.note = note
        self.retry_after = retry_after

        who = model or "provider"
        if status is None:
            message = f"{who}: {note or 'no answer'}"
        else:
            detail = self.body.replace("\n", " ") or "(empty body)"
            what = f"{note}: " if note else ""
            message = f"{who} returned HTTP {status}: {what}{detail}"
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        """Is a second try worth making? True for no answer, 408, 429 and every 5xx.

        403 is deliberately False here: the retry loop retries a first 403 once as a
        temporary experiment (our 403s have been intermittent and unexplained), so that
        exception lives in the loop, not in this permanent rule."""
        if self.status is None:  # checked first: None >= 500 would raise TypeError
            return True
        return self.status in _RETRYABLE_STATUS or self.status >= 500


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After arrives as a string of seconds; anything else means 'use normal backoff'."""
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:  # "abc" or an HTTP date
        return None


def _post_once(settings, payload: dict, timeout: float) -> dict:
    """One try. Raises ProviderError for anything unusable, including no answer at all."""
    model = payload.get("model", "")
    try:
        resp = httpx.post(
            f"{settings.gather_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.gather_api_key}"},
            json=payload,
            timeout=timeout,
        )
    except httpx.TransportError as exc:  # timeout, connect error, dropped connection
        raise ProviderError(None, model=model, note=f"no answer: {type(exc).__name__}") from exc
    if resp.is_error:  # any 4xx or 5xx
        raise ProviderError(
            resp.status_code,
            resp.text,
            model=model,
            retry_after=_parse_retry_after(resp.headers.get("retry-after")),
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(
            resp.status_code, resp.text, model=model, note="reply was not valid JSON"
        ) from exc


def _reason(err: ProviderError) -> str:
    """Short form for the span: the status, or what went wrong when nothing answered."""
    return str(err.status) if err.status is not None else (err.note or "no answer")


def post_chat_completion(
    settings, payload: dict, *, timeout: float = 90, sleep_fn=None, clock_fn=time.monotonic
) -> dict:
    """POST one chat-completions request and return the parsed JSON reply.

    Tries up to 3 times. Between tries it waits 0.5 s, then 1 s, or the server's
    Retry-After (capped at 30 s) if that is longer, and gives up rather than wait past a
    120 s deadline. Raises ProviderError when it gives up. `sleep_fn`/`clock_fn` exist so
    tests can run without really waiting (same idea as verify_recovery).

    Records `provider.attempts` (always) and `provider.failure_reasons` (one short entry
    per failed attempt, only if there were any) on the span the caller is inside, so a
    retried call is visible in Opik.
    """
    sleep_fn = sleep_fn or time.sleep  # looked up at call time, so tests can patch it
    started = clock_fn()
    span = trace.get_current_span()  # the span of whichever call site we are inside
    seen_403 = False
    attempt, reasons = 0, []  # set before the loop so `finally` can always read them
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return _post_once(settings, payload, timeout)
            except ProviderError as err:
                reasons.append(_reason(err))  # first, so every failure is listed

                # TEMPORARY: our 403s have been intermittent and unexplained, so retry the
                # first one once and log its body. Remove once the cause is known.
                is_first_403 = err.status == 403 and not seen_403
                seen_403 = seen_403 or err.status == 403

                if attempt == _MAX_ATTEMPTS or (not err.retryable and not is_first_403):
                    raise

                wait = _BACKOFF_START * 2 ** (attempt - 1)
                if err.retry_after is not None:
                    wait = max(wait, min(err.retry_after, _RETRY_AFTER_CAP))

                if clock_fn() - started + wait > _DEADLINE:
                    raise

                log.warning(
                    "provider call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, _MAX_ATTEMPTS, wait, err,
                )
                sleep_fn(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")  # for type checkers
    finally:  # runs on return AND on raise, so no exit path can skip it
        span.set_attribute("provider.attempts", attempt)
        if reasons:
            span.set_attribute("provider.failure_reasons", reasons)
