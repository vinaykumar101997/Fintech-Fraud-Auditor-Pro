"""Model provider factory.

The pipeline does not care who serves the model, so the provider is a config
switch rather than a rewrite. Set MODEL_PROVIDER=bedrock or vertex.

The one requirement that constrains model choice: the verdict comes back through
`with_structured_output`, which needs a model that supports tool use. On Bedrock
that means Anthropic Claude or Amazon Nova. Titan text models do not support tool
calling and will fail at the review stage.

Imports are lazy so that installing only one provider's SDK is enough.
"""

from __future__ import annotations

import logging
from typing import Any, Tuple

import config

logger = logging.getLogger(__name__)


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but not usable. Never swallowed."""


# Retries: utils.retry.call_with_retry (LLM_MAX_RETRIES) is the only retry
# layer. Left at their defaults, the SDKs retry inside each call as well
# (botocore's own retry handler, the Vertex client up to 6 retries), so one
# call could make many attempts, none of them visible in logs or call counts.
# Every client below is therefore built to make exactly one attempt per call.
VERTEX_MAX_RETRIES = 0
# botocore's retries["max_attempts"] counts retries AFTER the first request,
# not total attempts: checked against a local HTTP 429 server, 1 sent two
# requests per converse() call and 0 sent exactly one.
BEDROCK_MAX_RETRIES = 0


# --------------------------------------------------------------------------
# Bedrock
# --------------------------------------------------------------------------
def _bedrock_client_config() -> Any:
    """botocore Config making one HTTP request per call (no SDK-level retries)."""
    from botocore.config import Config

    return Config(retries={"max_attempts": BEDROCK_MAX_RETRIES, "mode": "standard"})


def _bedrock_chat() -> Any:
    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as exc:
        raise ProviderConfigError(
            "langchain-aws is not installed. Run: pip install -r requirements.txt"
        ) from exc

    # ChatBedrockConverse uses the Converse API, which normalises tool calling
    # across model families. ChatBedrock (the older class) does not, and
    # with_structured_output is less reliable on it.
    return ChatBedrockConverse(
        model=config.BEDROCK_CHAT_MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
        config=_bedrock_client_config(),
    )


def _bedrock_embeddings() -> Any:
    try:
        from langchain_aws import BedrockEmbeddings
    except ImportError as exc:
        raise ProviderConfigError("langchain-aws is not installed.") from exc

    return BedrockEmbeddings(
        model_id=config.BEDROCK_EMBED_MODEL_ID,
        region_name=config.AWS_REGION,
        model_kwargs={"dimensions": config.EMBEDDING_DIMENSIONS},
        config=_bedrock_client_config(),
    )


# --------------------------------------------------------------------------
# Vertex AI
# --------------------------------------------------------------------------
class OutputTruncated(RuntimeError):
    """A reply cut off at the output token limit. Transient to utils.retry.

    `usage` is the reply's token usage, so a meter can still count the call.
    """

    def __init__(self, usage: Any = None):
        super().__init__("Model reply was cut off at the output token limit (MAX_TOKENS).")
        self.usage = usage


TRUNCATION_SHARE = 0.99


def reject_truncated(result: Any, cap: Any = None) -> Any:
    """Return a chat result unchanged, or raise if a reply in it was cut off.

    A cut-off reply can still parse (a complete verdict, half a finding), so
    without this it would be used as if the model had finished.
    """
    for generation in result.generations:
        usage = getattr(generation.message, "usage_metadata", None)
        if (generation.generation_info or {}).get("finish_reason") == "MAX_TOKENS":
            raise OutputTruncated(usage)
        # Seen live: capped replies billed 8,183 of 8,192 tokens (reasoning included) with finish reason STOP.
        if cap and (usage or {}).get("output_tokens", 0) >= TRUNCATION_SHARE * cap:
            raise OutputTruncated(usage)
    return result


def _vertex_chat(narrative: bool = False) -> Any:
    try:
        from langchain_google_vertexai import ChatVertexAI
    except ImportError as exc:
        raise ProviderConfigError("langchain-google-vertexai is not installed.") from exc

    if not config.GCP_PROJECT_ID:
        raise ProviderConfigError("GCP_PROJECT_ID is not set.")

    # shortcut: only the synchronous path is guarded, guard _agenerate too if async calls are added
    class GuardedChatVertexAI(ChatVertexAI):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            return reject_truncated(super()._generate(*args, **kwargs), self.max_output_tokens)

    kwargs = {}
    if not narrative and config.VERTEX_THINKING_BUDGET is not None:
        kwargs["thinking_budget"] = config.VERTEX_THINKING_BUDGET
    return GuardedChatVertexAI(
        model_name=config.VERTEX_CHAT_MODEL,
        project=config.GCP_PROJECT_ID,
        location=config.GCP_LOCATION,
        temperature=config.LLM_TEMPERATURE,
        max_retries=VERTEX_MAX_RETRIES,
        max_output_tokens=(config.VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS if narrative
                           else config.VERTEX_MAX_OUTPUT_TOKENS),
        timeout=config.VERTEX_TIMEOUT_SECONDS,
        **kwargs,
    )


def _vertex_embeddings() -> Any:
    try:
        from langchain_google_vertexai import VertexAIEmbeddings
    except ImportError as exc:
        raise ProviderConfigError("langchain-google-vertexai is not installed.") from exc

    return VertexAIEmbeddings(
        model_name=config.VERTEX_EMBED_MODEL,
        project=config.GCP_PROJECT_ID,
        location=config.GCP_LOCATION,
        max_retries=VERTEX_MAX_RETRIES,
    )


_CHAT = {"bedrock": _bedrock_chat, "vertex": _vertex_chat}
_EMBED = {"bedrock": _bedrock_embeddings, "vertex": _vertex_embeddings}


def get_chat_model() -> Any:
    provider = config.MODEL_PROVIDER
    if provider not in _CHAT:
        raise ProviderConfigError(
            f"MODEL_PROVIDER='{provider}' is not recognised. Use one of: {', '.join(_CHAT)}"
        )
    logger.info("Chat model: %s via %s", config.chat_model_name(), provider)
    return _CHAT[provider]()


def get_narrative_model(review_model: Any) -> Any:
    """Model for the SAR draft and chat answers.

    On Vertex a second client with the higher output limit
    (VERTEX_NARRATIVE_MAX_OUTPUT_TOKENS); otherwise the review model itself.
    """
    return _vertex_chat(narrative=True) if config.MODEL_PROVIDER == "vertex" else review_model


def get_embeddings() -> Any:
    provider = config.MODEL_PROVIDER
    if provider not in _EMBED:
        raise ProviderConfigError(
            f"MODEL_PROVIDER='{provider}' is not recognised. Use one of: {', '.join(_EMBED)}"
        )
    return _EMBED[provider]()


def get_models() -> Tuple[Any, Any]:
    """Chat model and embeddings from the same provider."""
    return get_chat_model(), get_embeddings()


def describe() -> str:
    """One-line provider summary for logs and the UI status panel."""
    if config.MODEL_PROVIDER == "bedrock":
        return f"Bedrock {config.BEDROCK_CHAT_MODEL_ID} ({config.AWS_REGION})"
    return f"Vertex {config.VERTEX_CHAT_MODEL} ({config.GCP_LOCATION})"
