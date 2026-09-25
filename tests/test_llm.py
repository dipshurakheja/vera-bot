"""LLM layer: provider retries, polish validation, deterministic fallback, engine integration."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import NOW, push_dataset
from vera.config import Settings
from vera.llm.polish import Polisher, check_rewrite, parse_body
from vera.llm.provider import LLMError, LLMProvider, NullProvider, OpenAICompatibleProvider, build_provider
from vera.service import VeraEngine

DRAFT = "Dr. Meera, calls are down 50% this week. Want me to put 'Dental Cleaning @ ₹299' live today? Reply YES."


class Scripted(LLMProvider):
    name, model = "scripted", "scripted-1"

    def __init__(self, outputs, **kw):
        super().__init__(timeout=1, max_retries=kw.get("retries", 0))
        self.outputs = list(outputs)
        self.calls = 0

    def _call(self, prompt, system, max_tokens, timeout):
        self.calls += 1
        out = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        if isinstance(out, Exception):
            raise out
        return out


def test_valid_rewrite_is_accepted_and_cached():
    good = json.dumps({"body": "Dr. Meera, your calls fell 50% this week. Want me to put 'Dental Cleaning @ ₹299' live "
                               "on your profile today? Reply YES."})
    p = Polisher(Scripted([good]))
    body, src = p.polish(DRAFT, {"k": 1})
    assert src == "llm" and "fell 50%" in body
    body2, src2 = p.polish(DRAFT, {"k": 1})
    assert (body2, src2) == (body, "llm_cache") and p.provider.calls == 1


@pytest.mark.parametrize("bad,reason", [
    ({"body": "Dr. Meera, calls are down 65% this week. Want me to put 'Dental Cleaning @ ₹299' live today? Reply YES."},
     "new_numbers"),
    ({"body": "Dr. Meera, calls are down 50%. Want the offer live? Reply YES. Or maybe a post? Or a review ask?"},
     "extra_questions"),
    ({"body": "Dr. Meera, calls are down 50% this week — Smile Dental Hub nearby is winning. Want me to put 'Dental "
              "Cleaning @ ₹299' live today? Visit www.example.com Reply YES."}, "url"),
])
def test_invalid_rewrites_fall_back(bad, reason):
    p = Polisher(Scripted([json.dumps(bad)]))
    body, src = p.polish(DRAFT, {})
    assert body == DRAFT and src == "deterministic:validation"
    assert any(x.startswith(reason) for x in check_rewrite(DRAFT, bad["body"]))


def test_malformed_and_failing_outputs_fall_back():
    for out in ["not json at all", "{\"text\": \"x\"}", LLMError("timeout"), TimeoutError("slow")]:
        body, src = Polisher(Scripted([out])).polish(DRAFT, {})
        assert body == DRAFT and src.startswith("deterministic")


def test_provider_retries_then_raises():
    prov = Scripted([ConnectionError("down"), ConnectionError("down")], retries=1)
    with pytest.raises(LLMError):
        prov.generate("x")
    assert prov.calls == 2


def test_provider_recovers_on_retry():
    prov = Scripted([ConnectionError("down"), "ok"], retries=1)
    assert prov.generate("x") == "ok"


def test_parse_body_extracts_json():
    assert parse_body('Sure! {"body": "hello"} hope that helps') == "hello"
    with pytest.raises(LLMError):
        parse_body('{"body": ""}')


def test_null_and_misconfigured_providers():
    with pytest.raises(LLMError):
        NullProvider().generate("x")
    assert build_provider(Settings()) is None                     # off by default
    with pytest.raises(LLMError):
        OpenAICompatibleProvider("openai_compatible", "k")          # needs base url + model
    assert build_provider(Settings(llm_provider="openai_compatible", llm_api_key="k", llm_mode="polish")) is None


def test_engine_uses_valid_polish_and_rejects_invalid(seed):
    def run(provider):
        eng = VeraEngine(Settings(log_level="WARNING"))
        eng.polisher = Polisher(provider)
        eng._pool = ThreadPoolExecutor(max_workers=2)
        push_dataset(eng, seed)
        return eng.tick(NOW, ["trg_004_perf_dip_bharat"])["actions"][0]

    baseline = run(None)
    rewritten = baseline["body"].replace("The clearest gap:", "The main gap:")
    polished = run(Scripted([json.dumps({"body": rewritten})]))
    assert polished["body"] == rewritten and "Polished by LLM" in polished["rationale"]
    hallucinated = run(Scripted([json.dumps({"body": baseline["body"].replace("50%", "57%")})]))
    assert hallucinated["body"] == baseline["body"]
    broken = run(Scripted([LLMError("500")]))
    assert broken["body"] == baseline["body"]
