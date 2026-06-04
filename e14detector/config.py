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

# Size of the full national universe of E-14 actas. Used only to show public
# rollout progress ("X de Y actas sincronizadas"); override per-deployment.
NATIONAL_TOTAL_ACTAS = int(os.environ.get("E14_NATIONAL_TOTAL", "121913"))

# A stale snapshot pointer only means a *stalled* publisher while the rollout is still in
# progress; once the served set reaches ~this percent of the national total, the publisher
# has legitimately finished and stops flipping the pointer, so the stalled-publisher alert
# auto-suppresses (no hard switch needed). The 2% slack absorbs national-total estimate error.
ROLLOUT_COMPLETE_PCT = float(os.environ.get("E14_ROLLOUT_COMPLETE_PCT", "98"))

# A crop flagged by at least this many distinct voters gets a strong "muy reportada"
# badge regardless of the model verdict (the crowd signal stands on its own).
HIGH_VOTE_THRESHOLD = int(os.environ.get("E14_HIGH_VOTE_THRESHOLD", "100"))

# Canonical public base URL (no trailing slash) — used for SEO canonical links, Open
# Graph URLs, robots.txt and sitemap.xml. Override per-deployment once the domain is live.
SITE_URL = os.environ.get("E14_SITE_URL", "https://veeduria-ciudadana-elecciones-colombia-2026.com").rstrip("/")

# Base URL of the crop CDN (Fly Tigris / S3). When set, the public page serves crops
# from "<CDN>/crops/<file>" instead of the in-app /crop endpoint (which can't scale to
# ~1.58M files). Unset (the pilot) keeps the baked-in crops served via /crop.
CDN_BASE_URL = os.environ.get("E14_CDN_BASE_URL", "").rstrip("/")

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
# Two models by role. The LIVE poll path (upvote/downvote) values accuracy over speed,
# so it uses a heavier (thinking) model; the proactive 5%% pre-screen values speed + cost,
# so it uses a fast cheap model. Both ride OpenRouter (OpenAI-compatible).
# LIVE poll adjudication model. A 2026-06-02 bake-off (scripts/probe_models.py) ruled out
# reasoning VLMs (kimi-k2.5 never emits a verdict; step-3.7-flash is slow + false-positives)
# and kept gemma-4-31b: a clean one-word verdict, 20/20 specificity (never rubber-stamps a
# clean crop — vital since a STRANGE verdict is terminal/published). The poll runs it K times
# and publishes only on a majority (see POLL_CONSENSUS_K / webapp._review_crop_consensus).
OPENROUTER_MODEL = os.environ.get("E14_OPENROUTER_MODEL", "google/gemma-4-31b-it")
# Pre-screen model: Gemma proved more precise than gemini-flash-lite on these bilevel
# placeholder-dot crops (gemini over-flagged dots/zeros/asterisks), so the seed pass uses it.
SCREEN_MODEL = os.environ.get("E14_SCREEN_MODEL", "google/gemma-4-31b-it")
# Output caps. A thinking model needs room to reason before the JSON, so the live cap is
# generous; the screen model only emits the tiny JSON. 0 = uncapped.
OPENROUTER_MAX_TOKENS = int(os.environ.get("E14_OPENROUTER_MAX_TOKENS", "40"))
LIVE_MAX_TOKENS = int(os.environ.get("E14_LIVE_MAX_TOKENS", "1500"))
SCREEN_MAX_TOKENS = int(os.environ.get("E14_SCREEN_MAX_TOKENS", "200"))
# Confirm tier: a high-precision model re-checks ONLY the crops the cheap screen flagged,
# with the skeptical CONFIRM prompt. CLEAN here demotes the seed (drops it from the public
# basis); DIRTY keeps it. Only runs on ~2-3%% of the sample, so a strong model is affordable.
CONFIRM_MODEL = os.environ.get("E14_CONFIRM_MODEL", "anthropic/claude-sonnet-4.6")
CONFIRM_MAX_TOKENS = int(os.environ.get("E14_CONFIRM_MAX_TOKENS", "64"))
# OpenRouter provider-routing sort. For our tiny CLEAN/DIRTY answer, time-to-first-
# token dominates, so "latency" beats "throughput" (which optimizes tokens/sec we
# don't use, and was picking slow/flaky hosts). Accepts "latency"|"throughput"|"price".
OPENROUTER_SORT = os.environ.get("E14_OPENROUTER_SORT", "latency")
# Optional comma-separated provider allow-list (e.g. "deepinfra,nebius"). When set,
# OpenRouter is pinned to these hosts (order = the list). Empty = let OpenRouter pick.
OPENROUTER_PROVIDERS = [p.strip() for p in os.environ.get("E14_OPENROUTER_PROVIDERS", "").split(",") if p.strip()]

# --- Public community vote -------------------------------------------------
# DEPRECATED (poll/VLM adjudication): the swipe feed (see plans/pending/swipe-voting.md)
# made the *crowd* the verdict and removed the VLM-adjudication path from the web flow, so
# the POLL_*/APPEAL_*/*_CONSENSUS_*/*_PROMPT knobs below no longer affect the public site.
# They are kept only because PollConfig still reads them and the offline/admin VLM tools
# reference the prompts; they can be deleted once those are retired. Live knobs that DO
# still matter: RATE_*, FORM_*, HIGH_VOTE_THRESHOLD, HOTLIST_SIZE, VOTER_SALT, TURNSTILE_*.
POLL_THRESHOLD = int(os.environ.get("E14_POLL_THRESHOLD", "5"))
POLL_RESCALE_STEP = int(os.environ.get("E14_POLL_RESCALE_STEP", "5"))
# Self-consistency for the poll adjudication: read the crop K times at >0 temperature and
# require a MAJORITY to act. A stable anomaly (struck/slashed/over-drawn) votes the same way
# every time; the sub-pixel fused dot-on-digit class (information-undetectable from these
# bilevel crops, see e14-vlm-validation-2026-06) wobbles -> no majority -> stays re-eligible
# rather than being terminally published or wrongly cleared. K must be odd to avoid ties.
POLL_CONSENSUS_K = int(os.environ.get("E14_POLL_CONSENSUS_K", "3"))
POLL_CONSENSUS_TEMP = float(os.environ.get("E14_POLL_CONSENSUS_TEMP", "0.5"))
# Appeal path ("Se ve normal"): distinct normal-votes that trigger a NEUTRAL-prompt
# re-read of a crop currently shown as strange. A clean re-read un-publishes it; a
# still-strange one keeps it and re-opens only after another APPEAL_RESCALE_STEP.
APPEAL_THRESHOLD = int(os.environ.get("E14_APPEAL_THRESHOLD", str(POLL_THRESHOLD)))
APPEAL_RESCALE_STEP = int(os.environ.get("E14_APPEAL_RESCALE_STEP", str(POLL_RESCALE_STEP)))
# Optional override of the neutral appeal prompt (env). Empty = use the built-in
# balanced VOTE_FIELD_APPEAL_PROMPT. Set this to tune leniency without a redeploy.
APPEAL_PROMPT = os.environ.get("E14_APPEAL_PROMPT", "")
# Same, for the live poll's CONFIRM prompt (the one that decides publish-as-strange).
# Empty = built-in skeptical VOTE_FIELD_CONFIRM_PROMPT; set to make it stricter/looser.
CONFIRM_PROMPT = os.environ.get("E14_CONFIRM_PROMPT", "")
# Fraction of documents to pre-screen with the LLM (Gemma) in the national bulk
# pass. CV is dropped; cropping runs on all files, Gemma only on this sample.
LLM_SAMPLE_RATE = float(os.environ.get("E14_LLM_SAMPLE_RATE", "0.05"))
COMMUNITY_DB = os.environ.get("E14_COMMUNITY_DB", str(DEFAULT_OUTPUT_DIR / "community.sqlite"))
# Per-voter token-bucket rate limit (defeats casual scripted flooding).
RATE_REFILL_PER_MIN = float(os.environ.get("E14_RATE_REFILL_PER_MIN", "10"))
RATE_BUCKET = float(os.environ.get("E14_RATE_BUCKET", "20"))
# Short-TTL cache for public per-crop tallies (counts_among) on the render paths (feed deck,
# acta page, billboard cards). Keeps a popular acta/feed from making an Aurora (RDS Data API)
# round-trip per view. Separate from the 45s aggregate cache: tallies tolerate a few seconds of
# staleness (the vote response is already optimistic), so a small TTL collapses the read QPS to
# Aurora while staying fresh. 0 disables the cache (always query the store).
COUNTS_TTL = float(os.environ.get("E14_COUNTS_TTL", "8"))
# Cloudflare Turnstile (anti-bot). Secret verifies server-side; sitekey is public
# and rendered into the page. When the secret is empty, verification is skipped
# (local/dev), so the feature degrades gracefully offline.
TURNSTILE_SECRET = os.environ.get("E14_TURNSTILE_SECRET", "")
TURNSTILE_SITEKEY = os.environ.get("E14_TURNSTILE_SITEKEY", "")
# Explicit master switch: Turnstile only works on the real owned domain (not *.fly.dev),
# so keep it off until the custom domain serves and the widget is configured for it.
TURNSTILE_ENABLED = os.environ.get("E14_TURNSTILE_ENABLED", "").lower() in ("1", "true", "yes")

# Operator-only poll dashboard (/admin/poll): private vote counts + AI verdicts. Disabled
# (404) unless this token is set; access requires ?key=<token>.
ADMIN_TOKEN = os.environ.get("E14_ADMIN_TOKEN", "")

# --- Web hardening -------------------------------------------------------------------------
# Origins permitted to cast votes (comma-separated, e.g. "https://e14-poll.fly.dev,https://
# midomain.org"). When set, /api/vote rejects a request whose Origin header is present but not
# in this list — blocking cross-site (CSRF-style) auto-voting from another page using a
# visitor's session. Empty => not enforced (local dev / tests). A request with NO Origin (a
# non-browser client) is left to the rate-limit / Turnstile controls, not blocked here.
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("E14_ALLOWED_ORIGINS", "").split(",") if o.strip()]
# Interactive API docs (/docs, /redoc, /openapi.json) publish the full route+schema surface.
# Off by default (prod); set E14_EXPOSE_DOCS=1 for local exploration.
EXPOSE_DOCS = os.environ.get("E14_EXPOSE_DOCS", "").lower() in ("1", "true", "yes")
# Salt for the daily, rotating voter-identity hash (privacy: no raw IPs stored).
VOTER_SALT = os.environ.get("E14_VOTER_SALT", "e14-dev-salt")
# In-app bot check (replaces Turnstile when there is no owned domain). The acta page
# embeds a signed, timestamped token; the flag/appeal POST must echo it back un-forged
# and no faster than FORM_MIN_SECONDS after load. Reuses the voter salt as the HMAC key
# so no new secret is needed (it is already set in production).
FORM_TOKEN_SECRET = os.environ.get("E14_FORM_TOKEN_SECRET", "") or VOTER_SALT
FORM_MIN_SECONDS = float(os.environ.get("E14_FORM_MIN_SECONDS", "2"))
FORM_MAX_SECONDS = float(os.environ.get("E14_FORM_MAX_SECONDS", "3600"))


def ensure_output_dirs(output_dir: Path) -> None:
    """Create the detector's predictable output directory tree."""
    for rel in ("crops", "slots", "debug", "results", "review"):
        (Path(output_dir) / rel).mkdir(parents=True, exist_ok=True)
