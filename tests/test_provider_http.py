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
