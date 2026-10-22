"""The Bedrock / Continuum-gateway switch.

Continuum routes by model-name prefix and lets us register our own provider. Claude on Bedrock speaks
the same Messages API as Anthropic's own endpoint, and the anthropic SDK ships a Bedrock client — so the
Bedrock provider is Continuum's built-in AnthropicProvider with its two clients swapped. All of Continuum's
message/tool conversion and streaming code is reused unchanged.
"""

from __future__ import annotations

import os
from typing import Any

# Not `anthropic.AnthropicBedrock`: the package's __all__ leaves the Bedrock clients out, so type checkers
# (Pylance/pyright) report them as not exported. anthropic.lib.bedrock re-exports them explicitly.
from anthropic.lib.bedrock import AnthropicBedrock, AsyncAnthropicBedrock
from continuum.llm.providers import register_provider
from continuum.llm.providers.anthropic_provider import AnthropicProvider

from app.config.settings import settings


class BedrockClaudeProvider(AnthropicProvider):
    def __init__(self, region: str, profile: str | None = None):  # not calling super(): it would build api-key clients
        self._client = AnthropicBedrock(aws_region=region, aws_profile=profile)
        self._async_client = AsyncAnthropicBedrock(aws_region=region, aws_profile=profile)

    def _normalize_model(self, model: str) -> str:
        return model.removeprefix("bedrock/")

    def _build_kwargs(self, *args: Any, **kw: Any) -> dict[str, Any]:
        # anthropic SDK >= 1.x dropped `temperature`/`top_p` as typed arguments, but Continuum 2.1's
        # AnthropicProvider still passes them, which raises TypeError. The Bedrock Claude API still
        # accepts both in the request body, so move them into extra_body.
        kwargs = super()._build_kwargs(*args, **kw)
        sampling = {k: kwargs.pop(k) for k in ("temperature", "top_p") if k in kwargs}
        if sampling:
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), **sampling}
        return kwargs


def configure_llm() -> str:
    """Returns the model string the agent should use."""
    if settings.llm_backend == "bedrock":
        if os.environ.get("SMART_GATEWAY_URL"):
            raise RuntimeError("LLM_BACKEND=bedrock but SMART_GATEWAY_URL is set; the gateway would win. Empty it.")
        register_provider(
            "bedrock/", lambda _cfg, _settings: BedrockClaudeProvider(settings.aws_region, settings.aws_profile)
        )
        return f"bedrock/{settings.bedrock_model_id}"

    if settings.llm_backend == "gateway":
        if not os.environ.get("SMART_GATEWAY_URL"):
            raise RuntimeError("LLM_BACKEND=gateway needs SMART_GATEWAY_URL and SMART_GATEWAY_API_KEY.")
        # Pinned provider/model, never a bare name (the gateway rewrites bare names to an auto tier).
        return settings.gateway_model

    raise RuntimeError(f"Unknown LLM_BACKEND {settings.llm_backend!r}")
