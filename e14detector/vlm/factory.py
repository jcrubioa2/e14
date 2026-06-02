"""Select a VLM review provider from configuration.

Falls back to the deterministic mock provider whenever a real provider is not
configured (no API key), so the pipeline is always runnable offline and in tests.
"""
from __future__ import annotations

from .. import config
from .base import VisionReviewer
from .mock_provider import MockVisionReviewer


def _openrouter_routing() -> dict:
    """Build the OpenRouter ``provider`` routing hint from config.

    Sort by latency (TTFT dominates a tiny verdict). Optionally pin a provider
    allow-list, disabling fallbacks so we stay on the fast host(s) we chose.
    """
    routing: dict = {"sort": config.OPENROUTER_SORT}
    if config.OPENROUTER_PROVIDERS:
        # PREFER the fast hosts, but keep fallback ON so a 429/cold host rolls to the
        # next one instead of hard-failing (measured: pinning a fast host ~3-5s vs the
        # ~13s routing roulette; single-host-no-fallback hard-fails under burst).
        routing["order"] = config.OPENROUTER_PROVIDERS
        routing["allow_fallbacks"] = True
    return routing


def build_reviewer(provider: str | None = None) -> VisionReviewer:
    name = (provider or config.VLM_PROVIDER or "mock").lower()
    if name == "qwen" and config.QWEN_API_KEY:
        from .alibaba_qwen_provider import AlibabaQwenVisionReviewer

        return AlibabaQwenVisionReviewer(
            api_key=config.QWEN_API_KEY,
            base_url=config.QWEN_BASE_URL,
            model=config.QWEN_MODEL,
            timeout_seconds=config.QWEN_TIMEOUT_SECONDS,
            thinking_budget=config.QWEN_THINKING_BUDGET,
            max_image_px=config.QWEN_MAX_IMAGE_PX,
        )
    if name == "openrouter" and config.OPENROUTER_API_KEY:
        # Reuse the OpenAI-compatible adapter, but suppress the DashScope-only
        # ``response_format`` / ``enable_thinking`` payload fields that OpenRouter
        # rejects with a 400.
        from .alibaba_qwen_provider import AlibabaQwenVisionReviewer

        return AlibabaQwenVisionReviewer(
            api_key=config.OPENROUTER_API_KEY,
            base_url=config.OPENROUTER_BASE_URL,
            model=config.OPENROUTER_MODEL,
            timeout_seconds=config.QWEN_TIMEOUT_SECONDS,
            max_image_px=config.QWEN_MAX_IMAGE_PX,
            send_thinking=False,
            send_response_format=False,
            # A CLEAN/DIRTY verdict is tiny — cap output and route OpenRouter to the
            # fastest provider, since Gemma there is otherwise slow.
            max_tokens=config.OPENROUTER_MAX_TOKENS,
            provider_routing=_openrouter_routing(),
        )
    return MockVisionReviewer()
