"""The provider chain (9Router -> Groq -> OpenRouter -> Grok) as one chat model.

Port of lananh-main's provider chain: a failing provider is skipped, a
throttled or dead one rests so later calls go straight to a working one, and an
answer that merely fails to parse is the agent's problem, not a reason to
switch provider. No network: the providers are scripted models.
"""

import httpx
import openai
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

from tradingagents.llm_clients import fallback_client as fc
from tradingagents.llm_clients.factory import create_llm_client


def _status_error(cls, status, headers=None):
    request = httpx.Request("POST", "https://x/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return cls("boom", response=response, body=None)


class Scripted(BaseChatModel):
    """Answers with ``reply`` or raises ``error``; records each call."""

    name_: str
    reply: str = "ok"
    error: object = None
    calls: list = Field(default_factory=list)
    bound: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"{self.name_}:{self.reply}"))])

    def bind_tools(self, tools, **kwargs):
        self.bound.append([getattr(t, "__name__", t) for t in tools])
        return self

    def with_structured_output(self, schema, **kwargs):
        def parse(_):
            if self.error is not None:
                raise self.error
            return schema(answer=self.name_)
        return RunnableLambda(parse)


class Answer(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    fc.reset_provider_state()
    for env in ("PROVIDER_ORDER", "ROUTER9_BASE_URL", "ROUTER9_API_KEY", "GROQ_API_KEY",
                "OPENROUTER_API_KEY", "XAI_API_KEY", "LLM_QUOTA_COOLDOWN_SEC", "ROUTER9_RETRY_SEC"):
        monkeypatch.delenv(env, raising=False)
    yield
    fc.reset_provider_state()


def _model(*providers):
    return fc.FallbackChatModel(chain=[(p.name_, p) for p in providers])


@pytest.mark.unit
def test_the_first_provider_that_answers_wins():
    router9, groq = Scripted(name_="router9"), Scripted(name_="groq")
    assert _model(router9, groq).invoke("hi").content == "router9:ok"
    assert groq.calls == []


@pytest.mark.unit
def test_a_down_9router_falls_to_groq_and_rests():
    router9 = Scripted(name_="router9", error=openai.APIConnectionError(request=httpx.Request("POST", "https://x")))
    groq = Scripted(name_="groq")
    model = _model(router9, groq)

    assert model.invoke("hi").content == "groq:ok"
    assert fc.is_resting("router9")
    model.invoke("again")
    assert len(router9.calls) == 1          # skipped while it rests
    assert len(groq.calls) == 2


@pytest.mark.unit
def test_a_throttled_provider_rests_for_its_retry_after():
    groq = Scripted(name_="groq", error=_status_error(openai.RateLimitError, 429, {"retry-after": "42"}))
    openrouter = Scripted(name_="openrouter")
    assert _model(groq, openrouter).invoke("hi").content == "openrouter:ok"
    status = {row["name"]: row for row in [{"name": n, "resting": fc.is_resting(n)} for n in ("groq", "openrouter")]}
    assert status["groq"]["resting"] and not status["openrouter"]["resting"]


@pytest.mark.unit
def test_a_bad_request_is_skipped_without_resting():
    groq = Scripted(name_="groq", error=_status_error(openai.BadRequestError, 413))
    openrouter = Scripted(name_="openrouter")
    assert _model(groq, openrouter).invoke("hi").content == "openrouter:ok"
    assert not fc.is_resting("groq")


@pytest.mark.unit
def test_every_provider_resting_is_tried_anyway():
    router9 = Scripted(name_="router9")
    fc._rest("router9", 900, "down earlier")
    assert _model(router9).invoke("hi").content == "router9:ok"
    assert not fc.is_resting("router9")      # it answered, so it is back


@pytest.mark.unit
def test_every_provider_failing_names_each_failure():
    down = openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
    with pytest.raises(RuntimeError, match="9Router.*Groq"):
        _model(Scripted(name_="router9", error=down), Scripted(name_="groq", error=down)).invoke("hi")


@pytest.mark.unit
def test_a_non_provider_error_is_raised_not_hidden():
    broken = Scripted(name_="router9", error=ValueError("parse failed"))
    groq = Scripted(name_="groq")
    with pytest.raises(ValueError):
        _model(broken, groq).invoke("hi")
    assert groq.calls == []


@pytest.mark.unit
def test_tools_are_bound_on_whichever_provider_answers():
    router9 = Scripted(name_="router9", error=_status_error(openai.InternalServerError, 502))
    groq = Scripted(name_="groq")

    def get_stock_data():
        """Prices."""

    reply = _model(router9, groq).bind_tools([get_stock_data]).invoke("hi")
    assert reply.content == "groq:ok"
    assert groq.bound == [["get_stock_data"]]


@pytest.mark.unit
def test_structured_output_falls_through_providers():
    router9 = Scripted(name_="router9", error=_status_error(openai.InternalServerError, 503))
    groq = Scripted(name_="groq")
    assert _model(router9, groq).with_structured_output(Answer).invoke("hi") == Answer(answer="groq")


@pytest.mark.unit
def test_only_configured_providers_join_the_chain_in_order(monkeypatch):
    monkeypatch.setenv("PROVIDER_ORDER", "router9,grok,openrouter,groq")
    monkeypatch.setenv("ROUTER9_API_KEY", "k")          # no ROUTER9_BASE_URL: skipped
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert fc.configured_order() == ["xai", "groq"]
    monkeypatch.setenv("ROUTER9_BASE_URL", "https://9router.example/v1")
    assert fc.configured_order() == ["router9", "xai", "groq"]


@pytest.mark.unit
def test_the_factory_builds_the_chain_with_the_tier_model_on_9router(monkeypatch):
    monkeypatch.setenv("ROUTER9_BASE_URL", "https://9router.example/v1/")
    monkeypatch.setenv("ROUTER9_API_KEY", "r9")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    llm = create_llm_client("router9_chain", "gpt-4o").get_llm()
    names = [(name, model.model_name, str(model.openai_api_base)) for name, model in llm.chain]
    assert names == [
        ("router9", "gpt-4o", "https://9router.example/v1"),
        ("groq", "openai/gpt-oss-20b", "https://api.groq.com/openai/v1"),
    ]


@pytest.mark.unit
def test_no_provider_configured_is_a_clear_error():
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        fc.FallbackChatModel(chain=[]).invoke("hi")
