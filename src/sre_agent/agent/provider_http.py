"""The one place that talks to the OpenAI-compatible model provider (OpenRouter etc.).

Before this existed, two call sites each did `httpx.post(...)` then `raise_for_status()`.
raise_for_status() builds its message from the status line and URL only, so the server's
own explanation of a failure was thrown away. OpenRouter documents 403 as a permissions,
budget, guardrail or moderation problem and says which in the response body — and three of
thirteen attempts in the 2026-10-25 config C review died on a 403 whose cause we never
learned.
"""

from __future__ import annotations

import httpx

_BODY_CAP = 1000  # characters of the provider's reply kept in the error


class ProviderError(Exception):
    """The provider's reply could not be used, with the reason it gave.

    `status` is the HTTP status (200 when the body itself was unusable). The headers and
    the request are deliberately NOT kept: the API key travels in a header and the request
    holds cluster data (pod logs, events)."""

    def __init__(self, status: int, body: str, model: str = "", note: str = ""):
        self.status = status
        self.body = body.strip()[:_BODY_CAP]
        self.model = model
        self.note = note
        detail = self.body.replace("\n", " ") or "(empty body)"
        what = f"{note}: " if note else ""
        super().__init__(f"{model or 'provider'} returned HTTP {status}: {what}{detail}")


def post_chat_completion(settings, payload: dict, *, timeout: float = 90) -> dict:
    """POST one chat-completions request and return the parsed JSON reply.

    Raises ProviderError for any 4xx/5xx answer, and for a 2xx whose body is not JSON.
    Timeouts and connection failures (no reply at all, so no status) still raise httpx's
    own errors — whether to wrap those is a retry-policy decision, not made here.
    """
    resp = httpx.post(
        f"{settings.gather_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.gather_api_key}"},
        json=payload,
        timeout=timeout,
    )
    model = payload.get("model", "")
    if resp.is_error:  # any 4xx or 5xx
        raise ProviderError(resp.status_code, resp.text, model=model)
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(
            resp.status_code, resp.text, model=model, note="reply was not valid JSON"
        ) from exc
