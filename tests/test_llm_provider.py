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
