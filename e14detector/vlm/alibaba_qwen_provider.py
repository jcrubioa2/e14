"""Alibaba/Qwen VLM adapter (DashScope OpenAI-compatible endpoint).

Sends one or more cropped vote-field images plus the strict-JSON review prompt
and parses the model's reply with :func:`base.parse_vlm_json`. Uses ``requests``
(already a dependency) against the OpenAI-compatible ``/chat/completions`` route,
so no extra SDK is needed. The deep-thinking budget is passed through so reasoning
stays capped (per the preferred Qwen3.6-Flash configuration).
"""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path

import requests

from .base import VLMReviewResult, parse_vlm_json
from .prompt import VOTE_FIELD_REVIEW_PROMPT


def _data_uri(image_path: str, max_px: int = 0) -> str:
    """Return a base64 data URI for the crop, optionally downscaled.

    A vote-number crop is read fine well under a couple hundred pixels, so we
    shrink the long edge before upload to cut both transfer and vision-encode
    time. Falls back to the raw bytes if Pillow can't open the file.
    """
    raw = Path(image_path).read_bytes()
    if max_px and max_px > 0:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as img:
                if max(img.size) > max_px:
                    img = img.convert("RGB")
                    img.thumbnail((max_px, max_px), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG", optimize=True)
                    raw = buf.getvalue()
                    return f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"
        except Exception:
            pass  # any decode/resize failure: send the original bytes unchanged
    import mimetypes

    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class AlibabaQwenVisionReviewer:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: int = 60,
        thinking_budget: int = 300,
        max_image_px: int = 256,
        max_retries: int = 4,
        send_thinking: bool = True,
        send_response_format: bool = True,
        max_tokens: int | None = None,
        provider_routing: dict | None = None,
    ):
        if not api_key:
            raise ValueError("API key is required for the live VLM provider.")
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.thinking_budget = thinking_budget
        self.max_image_px = max_image_px
        self.max_retries = max_retries
        # DashScope accepts ``enable_thinking``/``thinking_budget`` and
        # ``response_format``; other OpenAI-compatible backends (OpenRouter) reject
        # them with a 400, so they can be suppressed per provider.
        self.send_thinking = send_thinking
        self.send_response_format = send_response_format
        # Speed levers: cap output length, and (OpenRouter) route to the fastest
        # provider. A verdict answer is tiny, so a low max_tokens cuts latency.
        self.max_tokens = max_tokens
        self.provider_routing = provider_routing

    def review_vote_field(
        self,
        image_paths: list[str],
        metadata: dict,
        thinking_budget: int | None = None,
        prompt_text: str | None = None,
    ) -> VLMReviewResult:
        budget = self.thinking_budget if thinking_budget is None else thinking_budget
        content: list[dict] = [{"type": "text", "text": prompt_text or VOTE_FIELD_REVIEW_PROMPT}]
        if metadata:
            context = ", ".join(f"{k}={v}" for k, v in metadata.items() if v is not None)
            if context:
                content.append({"type": "text", "text": f"Context: {context}"})
        for path in image_paths:
            if path and Path(path).exists():
                uri = _data_uri(path, self.max_image_px)
                content.append({"type": "image_url", "image_url": {"url": uri}})

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
        }
        if self.send_response_format:
            payload["response_format"] = {"type": "json_object"}
        if self.send_thinking:
            # DashScope deep-thinking controls. A budget of 0 disables reasoning.
            payload["enable_thinking"] = budget > 0
            payload["thinking_budget"] = max(budget, 0)
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.provider_routing:
            payload["provider"] = self.provider_routing  # OpenRouter routing hint
        text = self._post_with_retry(payload)
        return parse_vlm_json(text)

    def _post_with_retry(self, payload: dict) -> str:
        """POST with exponential backoff on rate limits / transient 5xx."""
        delay = 1.0
        for attempt in range(self.max_retries):
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                time.sleep(wait)
                delay = min(delay * 2, 16.0)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        raise RuntimeError("VLM request exhausted retries")
