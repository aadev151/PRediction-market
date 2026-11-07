import json
import math
from types import SimpleNamespace

import httpx2
import pytest
from anthropic import AsyncAnthropic

from prm.config import ServerConfig, TraderSpec
from prm.traders import llm
from prm.traders.llm import RATIONALE_MAX, Belief, LLMProvider, smoke, smoke_context

CFG = ServerConfig()
HAIKU = TraderSpec(name="diff_reader", model="claude-haiku-5-5")
CTX = smoke_context()
FORBIDDEN_PARAMS = {"temperature", "top_p", "top_k", "thinking"}


class _Messages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.resp


def _client(resp=None, exc=None):
    return SimpleNamespace(messages=_Messages(resp, exc))


def _resp(belief=None, stop="end_turn", in_tok=1000, out_tok=200):
    return SimpleNamespace(
        parsed_output=belief,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        stop_reason=stop,
    )


def _ticks():
    t = iter([10.0, 10.25])
    return lambda: next(t)


async def test_happy_path_and_exact_call_shape():
    client = _client(_resp(Belief(p_yes=0.7, rationale=" ok ", key_risks=["a"])))
    r = await LLMProvider(CFG, client, clock=_ticks()).belief(HAIKU, CTX)
    (call,) = client.messages.calls
    assert set(call) == {"model", "max_tokens", "system", "messages", "output_format"}
    assert not FORBIDDEN_PARAMS & set(call)
    assert call["model"] == "claude-haiku-5-5" and call["max_tokens"] == 4000
    assert call["output_format"] is Belief and call["system"] == CTX.system_prompt
    assert call["messages"] == [{"role": "user", "content": CTX.user_prompt}]
    assert (r.trader, r.p_yes, r.rationale, r.key_risks, r.error) == ("diff_reader", 0.7, "ok", ["a"], None)
    assert (r.input_tokens, r.output_tokens, r.latency_ms) == (1000, 200, 250)
    assert math.isclose(r.cost_usd, (1000 * 0.10 + 200 * 0.50) / 1e6)


@pytest.mark.parametrize(("raw", "clamped"), [(0.0, 0.02), (1.0, 0.98), (-3.0, 0.02), (0.5, 0.5)])
async def test_clamp(raw, clamped):
    r = await LLMProvider(CFG, _client(_resp(Belief(p_yes=raw, rationale="", key_risks=[])))).belief(
        HAIKU, CTX
    )
    assert r.p_yes == clamped


async def test_truncation():
    b = Belief(p_yes=0.5, rationale="x" * 2000, key_risks=["1", "2", "3", "4", "y" * 500])
    r = await LLMProvider(CFG, _client(_resp(b))).belief(HAIKU, CTX)
    assert len(r.rationale) == RATIONALE_MAX and r.key_risks == ["1", "2", "3"]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
async def test_bad_stop_reason_abstains_but_records_spend(stop):
    r = await LLMProvider(CFG, _client(_resp(Belief(p_yes=0.9, rationale="", key_risks=[]), stop))).belief(
        HAIKU, CTX
    )
    assert r.abstained and r.error == f"stop_reason={stop}"
    assert r.input_tokens == 1000 and r.cost_usd > 0


@pytest.mark.parametrize("parsed", [None, Belief(p_yes=float("nan"), rationale="", key_risks=[])])
async def test_missing_or_invalid_parse_abstains(parsed):
    r = await LLMProvider(CFG, _client(_resp(parsed))).belief(HAIKU, CTX)
    assert r.abstained and "no valid structured belief" in r.error


async def test_exception_abstains_never_raises():
    r = await LLMProvider(CFG, _client(exc=TimeoutError("slow")), clock=_ticks()).belief(HAIKU, CTX)
    assert r.abstained and r.error == "llm error: TimeoutError: slow" and r.latency_ms == 250


async def test_client_construction_failure_abstains(monkeypatch):
    def no_key():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(llm, "make_client", no_key)
    r = await LLMProvider(CFG).belief(HAIKU, CTX)
    assert r.abstained and "no credentials" in r.error


async def test_unpriced_model_costs_zero():
    spec = TraderSpec(name="diff_reader", model="some-new-model")
    r = await LLMProvider(CFG, _client(_resp(Belief(p_yes=0.5, rationale="", key_risks=[])))).belief(
        spec, CTX
    )
    assert r.cost_usd == 0.0 and not r.abstained


async def test_through_the_real_sdk_without_network():
    """The installed SDK accepts our exact call and fills parsed_output (HTTP is mocked)."""
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        text = json.dumps({"p_yes": 0.81, "rationale": "Looks fine.", "key_risks": ["none"]})
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 321, "output_tokens": 45},
            },
        )

    client = AsyncAnthropic(
        api_key="test-key-not-real",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    r = await LLMProvider(CFG, client).belief(HAIKU, CTX)
    assert (r.p_yes, r.rationale, r.key_risks, r.input_tokens, r.output_tokens) == (
        0.81,
        "Looks fine.",
        ["none"],
        321,
        45,
    )
    body = seen["body"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert set(body["output_config"]["format"]["schema"]["properties"]) == {"p_yes", "rationale", "key_risks"}
    assert not FORBIDDEN_PARAMS & set(body)


async def test_smoke_one_call_per_model():
    client = _client(_resp(Belief(p_yes=0.2, rationale="", key_risks=[])))
    results = await smoke(CFG, LLMProvider(CFG, client))
    assert sorted(r.model for r in results) == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert len(client.messages.calls) == 2
    assert "<untrusted_diff>" in client.messages.calls[0]["messages"][0]["content"]
