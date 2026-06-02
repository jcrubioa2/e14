"""Select a VLM review provider from configuration.

Falls back to the deterministic mock provider whenever a real provider is not
configured (no API key), so the pipeline is always runnable offline and in tests.
"""
from __future__ import annotations

from .. import config
from .base import VisionReviewer
from .mock_provider import MockVisionReviewer


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
        )
    return MockVisionReviewer()
