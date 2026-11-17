"""Provider clients make one attempt per call; call_with_retry owns retrying.

The SDK modules are replaced with fakes that record their constructor kwargs,
so this runs offline, without credentials and without either SDK installed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from utils import llm_provider  # noqa: E402


class _Recorder:
    """Stands in for an SDK class: records the kwargs it was built with."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeBotoConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_module(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def fake_sdks(monkeypatch):
    _fake_module(monkeypatch, "langchain_aws",
                 ChatBedrockConverse=type("ChatBedrockConverse", (_Recorder,), {}),
                 BedrockEmbeddings=type("BedrockEmbeddings", (_Recorder,), {}))
    _fake_module(monkeypatch, "botocore")
    _fake_module(monkeypatch, "botocore.config", Config=_FakeBotoConfig)
    _fake_module(monkeypatch, "langchain_google_vertexai",
                 ChatVertexAI=type("ChatVertexAI", (_Recorder,), {}),
                 VertexAIEmbeddings=type("VertexAIEmbeddings", (_Recorder,), {}))
    monkeypatch.setattr(config, "GCP_PROJECT_ID", "test-project")


@pytest.mark.parametrize("factory", ["_bedrock_chat", "_bedrock_embeddings"])
def test_bedrock_clients_do_not_retry(fake_sdks, factory):
    client = getattr(llm_provider, factory)()
    boto_config = client.kwargs["config"]
    assert isinstance(boto_config, _FakeBotoConfig)
    # botocore's max_attempts is the retry count after the first request:
    # 0 means one HTTP request per call (1 would send two).
    assert boto_config.kwargs["retries"] == {"max_attempts": 0, "mode": "standard"}


@pytest.mark.parametrize("factory", ["_vertex_chat", "_vertex_embeddings"])
def test_vertex_clients_do_not_retry(fake_sdks, factory):
    client = getattr(llm_provider, factory)()
    assert client.kwargs["max_retries"] == 0


# --- output limit, reasoning budget and timeout (Vertex only) ----------------
def test_vertex_review_model_gets_the_limit_and_timeout(fake_sdks, monkeypatch):
    monkeypatch.setattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 4096)
    monkeypatch.setattr(config, "VERTEX_TIMEOUT_SECONDS", 90.0)
    monkeypatch.setattr(config, "VERTEX_THINKING_BUDGET", None)
    kwargs = llm_provider._vertex_chat().kwargs
    assert kwargs["max_output_tokens"] == 4096
    assert kwargs["timeout"] == 90.0
    assert "thinking_budget" not in kwargs  # unset leaves the model's default


def test_vertex_reasoning_budget_reaches_the_review_model_only(fake_sdks, monkeypatch):
    monkeypatch.setattr(config, "VERTEX_THINKING_BUDGET", 0)
    assert llm_provider._vertex_chat().kwargs["thinking_budget"] == 0
    assert "thinking_budget" not in llm_provider._vertex_chat(narrative=True).kwargs


def test_narrative_model_has_its_own_limit_on_vertex(fake_sdks, monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "vertex")
    monkeypatch.setattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 4096)
    monkeypatch.setattr(config, "VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS", 20000)
    review = llm_provider.get_chat_model()
    narrative = llm_provider.get_narrative_model(review)
    assert narrative is not review
    assert review.kwargs["max_output_tokens"] == 4096
    assert narrative.kwargs["max_output_tokens"] == 20000
    assert narrative.kwargs["timeout"] == config.VERTEX_TIMEOUT_SECONDS


def test_bedrock_is_untouched_by_the_vertex_settings(fake_sdks, monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "bedrock")
    review = llm_provider.get_chat_model()
    assert llm_provider.get_narrative_model(review) is review
    assert set(review.kwargs) == {"model", "region_name", "temperature", "max_tokens", "config"}
    assert review.kwargs["max_tokens"] == config.LLM_MAX_TOKENS


@pytest.mark.parametrize("name, value", [
    ("VERTEX_MAX_OUTPUT_TOKENS", "1023"),
    ("VERTEX_MAX_OUTPUT_TOKENS", "65536"),
    ("VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS", "100"),
    ("VERTEX_THINKING_BUDGET", "-2"),
    ("VERTEX_TIMEOUT_SECONDS", "0"),
    ("VERTEX_MAX_OUTPUT_TOKENS", "many"),
])
def test_invalid_vertex_settings_stop_startup(monkeypatch, name, value):
    import importlib

    with monkeypatch.context() as mp:
        mp.setenv(name, value)
        with pytest.raises(ValueError):
            importlib.reload(config)
    importlib.reload(config)  # back to the defaults for the tests that follow


def test_vertex_setting_defaults(monkeypatch):
    import importlib

    for name in ("VERTEX_MAX_OUTPUT_TOKENS", "VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS",
                 "VERTEX_THINKING_BUDGET", "VERTEX_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(config)
    assert 1024 <= config.VERTEX_MAX_OUTPUT_TOKENS < config.VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS <= 65535
    assert config.VERTEX_THINKING_BUDGET is None
    assert config.VERTEX_TIMEOUT_SECONDS == 120.0


# --- a reply cut off at the limit is never used ------------------------------
# The fake below stands in for the SDK's chat class; _vertex_chat() wraps it
# with the same guard it puts on the real one, and the review runs on top.
core = pytest.importorskip("langchain_core")
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

VERDICT = {"verdict": "CLEAR", "confidence": 0.9, "rationale": "Nothing unusual."}


def reply(text="", verdict=False, finish="STOP", output=7):
    calls = [{"name": "TransactionAssessment", "args": VERDICT, "id": "1"}] if verdict else []
    return {"text": text, "calls": calls, "finish": finish, "output": output}


class ScriptedChat(BaseChatModel):
    """Answers a call that binds the verdict schema from `review`, any other from `first`."""

    model_config = {"extra": "allow"}
    first: dict = reply("A complete finding.")
    review: dict = reply(verdict=True)
    seen: list = []

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        names = [getattr(t, "__name__", "") for t in tools]
        return self.bind(is_review="TransactionAssessment" in names)

    def _generate(self, messages, stop=None, run_manager=None, is_review=False, **kwargs):
        script = self.review if is_review else self.first
        self.seen.append("review" if is_review else "first")
        message = AIMessage(content=script["text"], tool_calls=script["calls"],
                            usage_metadata={"input_tokens": 10, "output_tokens": script["output"],
                                            "total_tokens": 10 + script["output"]})
        return ChatResult(generations=[
            ChatGeneration(message=message, generation_info={"finish_reason": script["finish"]})])


@pytest.fixture
def guarded(monkeypatch):
    """Builds the guarded model through _vertex_chat(), over the scripted fake."""
    _fake_module(monkeypatch, "langchain_google_vertexai", ChatVertexAI=ScriptedChat)
    monkeypatch.setattr(config, "GCP_PROJECT_ID", "test-project")
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr("utils.retry.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 8192)
    monkeypatch.setattr(config, "VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS", 32768)

    def build(narrative=False, **script):
        return llm_provider._vertex_chat(narrative).model_copy(update={**script, "seen": []})
    return build


def _fixed(model):
    from langchain_core.runnables import RunnableLambda
    from utils.agents import ComplianceAuditEngine
    from utils.rag_chain import build_analysis_chain

    chain = build_analysis_chain(model, RunnableLambda(lambda query: []))
    return ComplianceAuditEngine(llm=model).assess_transaction(
        transaction_id="T1", amount=100, country="US", rag_engine=chain)


def _agent(model):
    from utils.investigation import InvestigationEngine

    return InvestigationEngine(llm=model).assess_transaction(
        transaction_id="T1", amount=100, country="US")


CUT_OFF = {
    "first call empty": {"first": reply(finish="MAX_TOKENS")},
    "first call cut off mid-sentence": {"first": reply("The payment appears to", finish="MAX_TOKENS")},
    "complete verdict in a cut-off reply": {"review": reply(verdict=True, finish="MAX_TOKENS")},
    # Seen live: a normal stop billed 8,183 of the 8,192 cap.
    "first call at the cap with a normal stop": {"first": reply("A complete finding.", output=8183)},
    "verdict at the cap with a normal stop": {"review": reply(verdict=True, output=8183)},
}


@pytest.mark.parametrize("run", [_fixed, _agent], ids=["fixed", "agent"])
def test_a_complete_reply_still_gets_its_verdict(guarded, run):
    model = guarded()
    assert run(model).verdict.value == "CLEAR"
    assert model.seen == ["first", "review"]


@pytest.mark.parametrize("run", [_fixed, _agent], ids=["fixed", "agent"])
@pytest.mark.parametrize("script", CUT_OFF.values(), ids=CUT_OFF.keys())
def test_a_cut_off_reply_ends_as_error_never_clear(guarded, run, script):
    model = guarded(**script)
    outcome = run(model)
    assert outcome.verdict.value == "ERROR"
    assert "OutputTruncated" in outcome.error_detail
    # Retried like any transient failure, then given up on: 1 + LLM_MAX_RETRIES attempts.
    cut = "first" if "first" in script else "review"
    assert model.seen.count(cut) == 1 + config.LLM_MAX_RETRIES


def test_cut_off_error_carries_usage_and_is_retryable():
    from utils.retry import is_rate_limited, is_terminal

    result = ChatResult(generations=[ChatGeneration(
        message=AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 2,
                                                      "total_tokens": 3}),
        generation_info={"finish_reason": "MAX_TOKENS"})])
    with pytest.raises(llm_provider.OutputTruncated) as caught:
        llm_provider.reject_truncated(result)
    assert caught.value.usage["total_tokens"] == 3
    assert not is_terminal(caught.value) and not is_rate_limited(caught.value)


@pytest.mark.parametrize("narrative, output, cut", [
    (False, 8183, True),     # review cap 8,192: the live case
    (False, 8000, False),
    (True, 8183, False),     # narrative cap 32,768: its own threshold
    (True, 32441, True),
    (True, 32000, False),
])
def test_a_normal_stop_near_the_models_own_cap_is_cut_off(guarded, narrative, output, cut):
    model = guarded(narrative=narrative, first=reply("Done.", output=output))
    if cut:
        with pytest.raises(llm_provider.OutputTruncated) as caught:
            model.invoke("hi")
        assert caught.value.usage["output_tokens"] == output
    else:
        assert model.invoke("hi").content == "Done."
