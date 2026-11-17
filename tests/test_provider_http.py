"""provider_http: the shared model-provider call and its error type.

Real bug (2026-10-25 config C review): 3 of 13 attempts died on an OpenRouter 403 and
nothing recorded why, because raise_for_status() keeps only the status line and URL and our
code never read the response body. Pure — no network; the HTTP call is faked."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sre_agent.agent import graph
from sre_agent.agent.provider_http import ProviderError, post_chat_completion
from sre_agent.config import Settings

_REQUEST = httpx.Request("POST", "https://example.test/v1/chat/completions")
_KEY = "sk-secret-key"


def _settings() -> Settings:
    return Settings(
        anthropic_api_key="x",
        gather_api_key=_KEY,
        gather_base_url="https://example.test/v1",
        gather_model="m",
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The retry loop waits between tries; no test here should really wait."""
    monkeypatch.setattr("sre_agent.agent.provider_http.time.sleep", lambda _s: None)


def _fake_post(monkeypatch, response: httpx.Response) -> dict:
    """Replace httpx.post; returns a dict that records how it was called."""
    seen: dict = {}

    def fake(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return response

    monkeypatch.setattr(httpx, "post", fake)
    return seen


def test_success_returns_the_parsed_reply_and_calls_the_right_endpoint(monkeypatch):
    seen = _fake_post(monkeypatch, httpx.Response(200, json={"choices": []}, request=_REQUEST))
    assert post_chat_completion(_settings(), {"model": "m"}) == {"choices": []}
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["headers"] == {"Authorization": f"Bearer {_KEY}"}


def test_error_response_keeps_the_status_and_the_reason(monkeypatch):
    body = {"error": {"code": 403, "message": "input flagged", "metadata": {"reasons": ["x"]}}}
    _fake_post(monkeypatch, httpx.Response(403, json=body, request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        post_chat_completion(_settings(), {"model": "m"})
    assert err.value.status == 403
    assert "input flagged" in err.value.body
    # the text the eval prints as "run error: ..." must carry the reason too
    assert "403" in str(err.value) and "input flagged" in str(err.value) and "m" in str(err.value)


def test_server_errors_are_wrapped_too(monkeypatch):
    _fake_post(monkeypatch, httpx.Response(503, text="<html>no provider</html>", request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        post_chat_completion(_settings(), {"model": "m"})
    assert err.value.status == 503


def test_a_huge_body_is_capped(monkeypatch):
    _fake_post(monkeypatch, httpx.Response(403, text="x" * 5000, request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        post_chat_completion(_settings(), {"model": "m"})
    assert len(err.value.body) == 1000


def test_an_empty_error_body_is_still_readable(monkeypatch):
    _fake_post(monkeypatch, httpx.Response(502, text="", request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        post_chat_completion(_settings(), {"model": "m"})
    assert "(empty body)" in str(err.value)


def test_a_200_whose_body_is_not_json_is_a_provider_error(monkeypatch):
    _fake_post(monkeypatch, httpx.Response(200, text="<html>oops</html>", request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        post_chat_completion(_settings(), {"model": "m"})
    assert err.value.status == 200
    assert "not valid JSON" in str(err.value) and "oops" in str(err.value)


def test_the_api_key_never_ends_up_in_the_error(monkeypatch):
    _fake_post(monkeypatch, httpx.Response(401, json={"error": {"message": "bad key"}}, request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        post_chat_completion(_settings(), {"model": "m"})
    assert _KEY not in str(err.value) and _KEY not in err.value.body


def test_the_gather_call_goes_through_the_helper(monkeypatch):
    _fake_post(monkeypatch, httpx.Response(403, json={"error": {"message": "nope"}}, request=_REQUEST))
    with pytest.raises(ProviderError) as err:
        graph._gather_call_open_model(_settings(), [])
    assert err.value.status == 403


def test_graph_makes_no_provider_call_of_its_own():
    # Both provider calls (gather and the analyze stages) must go through the helper, so a
    # retry policy added there covers everything. The analyze call is nested inside
    # _build_graph and cannot be reached directly, hence this source-level guard.
    source = Path(graph.__file__).read_text(encoding="utf-8")
    assert "httpx.post" not in source and "raise_for_status" not in source


@pytest.mark.parametrize(
    "status, expected",
    [
        (None, True), (408, True), (429, True), (500, True), (503, True),
        (400, False), (401, False), (402, False), (403, False), (404, False),
    ],
)
def test_retryable_by_status(status, expected):
    assert ProviderError(status, "x").retryable is expected


def test_no_answer_message_has_no_http():
    err = ProviderError(None, model="m", note="no answer: ReadTimeout")
    assert "HTTP" not in str(err)
    assert "ReadTimeout" in str(err)


def test_retry_after_defaults_to_none():
    assert ProviderError(503, "x").retry_after is None
    assert ProviderError(429, "x", retry_after=20.0).retry_after == 20.0


# --- retry loop -------------------------------------------------------------------------
# sleep_fn/clock_fn are injected (same idea as verify_recovery) so none of these wait.


def _resp(status, **kw):
    return httpx.Response(status, request=_REQUEST, **kw)


def _sequence(monkeypatch, *replies):
    """Fake provider: each call takes the next item; an exception item is raised."""
    queue, calls = list(replies), []

    def fake(url, **kwargs):
        calls.append(url)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(httpx, "post", fake)
    return calls  # len(calls) = number of attempts made


def _call(**kw):
    return post_chat_completion(_settings(), {"model": "m"}, **kw)


def test_success_on_first_try_never_waits(monkeypatch):
    calls, waits = _sequence(monkeypatch, _resp(200, json={"ok": True})), []
    assert _call(sleep_fn=waits.append) == {"ok": True}
    assert len(calls) == 1 and waits == []


def test_503_then_200_is_retried(monkeypatch):
    calls = _sequence(monkeypatch, _resp(503, text="busy"), _resp(200, json={"ok": True}))
    waits = []
    assert _call(sleep_fn=waits.append) == {"ok": True}
    assert len(calls) == 2 and waits == [0.5]


def test_401_gives_up_at_once(monkeypatch):
    calls, waits = _sequence(monkeypatch, _resp(401, text="bad key")), []
    with pytest.raises(ProviderError) as err:
        _call(sleep_fn=waits.append)
    assert err.value.status == 401 and len(calls) == 1 and waits == []


def test_first_403_is_retried_once(monkeypatch):
    calls = _sequence(monkeypatch, _resp(403, text="blip"), _resp(200, json={"ok": True}))
    waits = []
    assert _call(sleep_fn=waits.append) == {"ok": True}
    assert len(calls) == 2 and waits == [0.5]


def test_second_403_gives_up(monkeypatch):
    calls = _sequence(
        monkeypatch, _resp(403, text="no"), _resp(403, text="no"), _resp(200, json={"ok": True})
    )
    waits = []
    with pytest.raises(ProviderError) as err:
        _call(sleep_fn=waits.append)
    assert err.value.status == 403
    assert len(calls) == 2 and waits == [0.5]  # the 200 is never asked for


def test_three_503s_give_up_after_three_attempts(monkeypatch):
    calls = _sequence(monkeypatch, *[_resp(503, text="busy")] * 3)
    waits = []
    with pytest.raises(ProviderError) as err:
        _call(sleep_fn=waits.append)
    assert err.value.status == 503
    assert len(calls) == 3 and waits == [0.5, 1.0]


def test_timeout_is_retried_then_succeeds(monkeypatch):
    calls = _sequence(monkeypatch, httpx.ReadTimeout("slow"), _resp(200, json={"ok": True}))
    waits = []
    assert _call(sleep_fn=waits.append) == {"ok": True}
    assert len(calls) == 2 and waits == [0.5]


def test_persistent_timeouts_raise_a_no_answer_error(monkeypatch):
    _sequence(monkeypatch, *[httpx.ConnectTimeout("x")] * 3)
    with pytest.raises(ProviderError) as err:
        _call(sleep_fn=lambda _s: None)
    assert err.value.status is None and "ConnectTimeout" in str(err.value)


def test_retry_after_is_obeyed(monkeypatch):
    _sequence(
        monkeypatch,
        _resp(429, text="slow", headers={"retry-after": "20"}),
        _resp(200, json={"ok": True}),
    )
    waits = []
    _call(sleep_fn=waits.append)
    assert waits == [20.0]


def test_retry_after_is_capped_at_30_seconds(monkeypatch):
    _sequence(
        monkeypatch,
        _resp(429, text="slow", headers={"retry-after": "3600"}),
        _resp(200, json={"ok": True}),
    )
    waits = []
    _call(sleep_fn=waits.append)
    assert waits == [30.0]


def test_a_garbage_retry_after_falls_back_to_normal_backoff(monkeypatch):
    _sequence(
        monkeypatch,
        _resp(429, text="slow", headers={"retry-after": "abc"}),
        _resp(200, json={"ok": True}),
    )
    waits = []
    _call(sleep_fn=waits.append)
    assert waits == [0.5]


def test_gives_up_instead_of_waiting_past_the_deadline(monkeypatch):
    # clock reads: 0 at the start, 105 s at the first failure. 105 + 20 > 120, so no retry.
    calls = _sequence(
        monkeypatch,
        _resp(429, text="slow", headers={"retry-after": "20"}),
        _resp(200, json={"ok": True}),
    )
    times, waits = iter([0.0, 105.0]), []
    with pytest.raises(ProviderError) as err:
        _call(sleep_fn=waits.append, clock_fn=lambda: next(times))
    assert err.value.status == 429 and len(calls) == 1 and waits == []
