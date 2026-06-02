"""Configuration defaults for the local E-14 detector."""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a project-root .env into os.environ.

    Intentionally dependency-free. Existing environment variables win, so an
    explicit ``export`` still overrides the file. Lines starting with ``#`` and
    blank lines are ignored; surrounding quotes on values are stripped.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

DEFAULT_INPUT_DIR = Path("data") / "actas"
DEFAULT_OUTPUT_DIR = Path("data") / "detector"
DEFAULT_RESULTS_DB = DEFAULT_OUTPUT_DIR / "results" / "results.sqlite"
DEFAULT_RESULTS_JSONL = DEFAULT_OUTPUT_DIR / "results" / "results.jsonl"

DEFAULT_DPI = 300
DEFAULT_WORKERS = 4
DEFAULT_VLM_MODE = "off"
DEFAULT_GPU_MODE = os.environ.get("E14_USE_GPU", "auto").lower()
DEFAULT_PAGES = (1, 2)

GPU_MODES = ("off", "auto", "opencl", "directml", "rocm")
VLM_MODES = ("off", "on", "suspicious-only")

# Optional Qwen (Alibaba DashScope, OpenAI-compatible) review provider. All
# read from the environment so no secret lives in the repo; when the API key is
# absent the factory falls back to the deterministic mock provider.
VLM_PROVIDER = os.environ.get("E14_VLM_PROVIDER", "mock").lower()  # "mock" | "qwen"
QWEN_API_KEY = os.environ.get("E14_QWEN_API_KEY")
QWEN_BASE_URL = os.environ.get(
    "E14_QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL = os.environ.get("E14_QWEN_MODEL", "qwen-vl-plus")
# Deep-thinking budget. Reading three digit slots rarely needs long reasoning, so
# the default is intentionally small for low per-call latency. The two-tier pass
# (below) re-runs only ambiguous rows with the larger ``escalate`` budget.
QWEN_THINKING_BUDGET = int(os.environ.get("E14_QWEN_THINKING_BUDGET", "300"))
# Larger budget used only when escalating an UNCLEAR fast-pass result.
QWEN_ESCALATE_THINKING_BUDGET = int(os.environ.get("E14_QWEN_ESCALATE_THINKING_BUDGET", "1200"))
# When set, the first pass runs with thinking disabled (fastest); only rows that
# come back UNCLEAR are re-reviewed with the escalate budget. Disable to always
# use the single-budget path.
VLM_TWO_TIER = os.environ.get("E14_VLM_TWO_TIER", "1") not in ("0", "false", "False", "")
# Downscale long edge before upload (px). Digit crops read fine well under this;
# smaller payloads cut upload + vision-encode time. 0 disables resizing.
QWEN_MAX_IMAGE_PX = int(os.environ.get("E14_QWEN_MAX_IMAGE_PX", "256"))
QWEN_TIMEOUT_SECONDS = int(os.environ.get("E14_QWEN_TIMEOUT", "60"))
# Concurrency for the network-bound VLM pass (threads, not processes).
VLM_CONCURRENCY = int(os.environ.get("E14_VLM_CONCURRENCY", "16"))

# --- OpenRouter (OpenAI-compatible) live-review provider --------------------
# Used by the public report's community-poll adjudication. OpenRouter rejects the
# DashScope-only ``enable_thinking``/``response_format`` keys, so the factory builds
# the shared Qwen adapter with those payload fields suppressed (see vlm/factory.py).
OPENROUTER_API_KEY = os.environ.get("E14_OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.environ.get("E14_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("E14_OPENROUTER_MODEL", "qwen/qwen-2.5-vl-7b-instruct")
# Cap the answer length (a CLEAN/DIRTY verdict is tiny) to cut latency + output cost.
OPENROUTER_MAX_TOKENS = int(os.environ.get("E14_OPENROUTER_MAX_TOKENS", "40"))
# OpenRouter provider-routing sort. For our tiny CLEAN/DIRTY answer, time-to-first-
# token dominates, so "latency" beats "throughput" (which optimizes tokens/sec we
# don't use, and was picking slow/flaky hosts). Accepts "latency"|"throughput"|"price".
OPENROUTER_SORT = os.environ.get("E14_OPENROUTER_SORT", "latency")
# Optional comma-separated provider allow-list (e.g. "deepinfra,nebius"). When set,
# OpenRouter is pinned to these hosts (order = the list). Empty = let OpenRouter pick.
OPENROUTER_PROVIDERS = [p.strip() for p in os.environ.get("E14_OPENROUTER_PROVIDERS", "").split(",") if p.strip()]

# --- Public community-flag poll --------------------------------------------
# The public report lets anyone flag a candidate crop. Crossing the threshold only
# *triggers* a VLM second opinion; the VLM (not the crowd) decides what is published.
# A VLM "clean" verdict un-publishes but stays re-eligible: if distinct votes climb
# by another RESCALE_STEP it is re-adjudicated, so one flaky "clean" cannot bury a
# real anomaly forever. STRANGE is terminal/published.
POLL_THRESHOLD = int(os.environ.get("E14_POLL_THRESHOLD", "5"))
POLL_RESCALE_STEP = int(os.environ.get("E14_POLL_RESCALE_STEP", "5"))
# Appeal path ("Se ve normal"): distinct normal-votes that trigger a NEUTRAL-prompt
# re-read of a crop currently shown as strange. A clean re-read un-publishes it; a
# still-strange one keeps it and re-opens only after another APPEAL_RESCALE_STEP.
APPEAL_THRESHOLD = int(os.environ.get("E14_APPEAL_THRESHOLD", str(POLL_THRESHOLD)))
APPEAL_RESCALE_STEP = int(os.environ.get("E14_APPEAL_RESCALE_STEP", str(POLL_RESCALE_STEP)))
# Optional override of the neutral appeal prompt (env). Empty = use the built-in
# balanced VOTE_FIELD_APPEAL_PROMPT. Set this to tune leniency without a redeploy.
APPEAL_PROMPT = os.environ.get("E14_APPEAL_PROMPT", "")
# Fraction of documents to pre-screen with the LLM (Gemma) in the national bulk
# pass. CV is dropped; cropping runs on all files, Gemma only on this sample.
LLM_SAMPLE_RATE = float(os.environ.get("E14_LLM_SAMPLE_RATE", "0.05"))
COMMUNITY_DB = os.environ.get("E14_COMMUNITY_DB", str(DEFAULT_OUTPUT_DIR / "community.sqlite"))
# Per-voter token-bucket rate limit (defeats casual scripted flooding).
RATE_REFILL_PER_MIN = float(os.environ.get("E14_RATE_REFILL_PER_MIN", "10"))
RATE_BUCKET = float(os.environ.get("E14_RATE_BUCKET", "20"))
# Cloudflare Turnstile (anti-bot). Secret verifies server-side; sitekey is public
# and rendered into the page. When the secret is empty, verification is skipped
# (local/dev), so the feature degrades gracefully offline.
TURNSTILE_SECRET = os.environ.get("E14_TURNSTILE_SECRET", "")
TURNSTILE_SITEKEY = os.environ.get("E14_TURNSTILE_SITEKEY", "")
# Salt for the daily, rotating voter-identity hash (privacy: no raw IPs stored).
VOTER_SALT = os.environ.get("E14_VOTER_SALT", "e14-dev-salt")


def ensure_output_dirs(output_dir: Path) -> None:
    """Create the detector's predictable output directory tree."""
    for rel in ("crops", "slots", "debug", "results", "review"):
        (Path(output_dir) / rel).mkdir(parents=True, exist_ok=True)
