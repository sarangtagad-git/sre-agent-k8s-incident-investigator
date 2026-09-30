"""The gather/all-stages open-model pricing table (graph.py's _GATHER_MODEL_PRICES).

Real bug, found 2026-09-30: gather_price_in/gather_price_out held Qwen2.5-72B's August
price but were silently applied to DeepSeek V4.1 Flash too once config C (all_stages_model)
started pricing every stage through the same fallback, not just gather's small slice.
Fix: a per-model price table, with a loud (warnings.warn) fallback instead of a silent
one for any model not yet measured — see [[gather-model-swap]]. Pure — no cluster, no
API key, no live model call needed."""

from __future__ import annotations

import pytest

from sre_agent.agent.graph import _estimate_gather_cost, _price_for_gather
from sre_agent.config import Settings


def _settings(**kw) -> Settings:
    base = {"anthropic_api_key": "sk-test", "gather_price_in": 0.36, "gather_price_out": 0.40}
    base.update(kw)
    return Settings(**base)


def test_known_model_uses_its_own_measured_price_no_warning(recwarn):
    price_in, price_out = _price_for_gather("deepseek/deepseek-v4.1-flash", _settings())
    assert (price_in, price_out) == (0.02, 0.60)
    assert len(recwarn) == 0


def test_another_known_model_is_unaffected_by_the_first(recwarn):
    price_in, price_out = _price_for_gather("qwen/qwen-2.5-72b-instruct", _settings())
    assert (price_in, price_out) == (0.36, 0.40)
    assert len(recwarn) == 0


def test_unknown_model_falls_back_loudly_not_silently():
    with pytest.warns(UserWarning, match="no measured OpenRouter price"):
        price_in, price_out = _price_for_gather("some/brand-new-model", _settings())
    assert (price_in, price_out) == (0.36, 0.40)  # the configured fallback, used knowingly


def test_estimate_gather_cost_uses_the_real_deepseek_price():
    settings = _settings(gather_model="deepseek/deepseek-v4.1-flash")
    totals = {"input": 12_071, "output": 1_090}
    cost = _estimate_gather_cost(totals, settings)
    expected = (12_071 * 0.02 + 1_090 * 0.60) / 1e6
    assert cost == pytest.approx(expected)
    # and NOT the stale Qwen price this bug used to silently apply:
    stale = (12_071 * 0.36 + 1_090 * 0.40) / 1e6
    assert cost != pytest.approx(stale)
