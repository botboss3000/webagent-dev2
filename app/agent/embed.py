"""
Embedding utility for WebAgent memory system.

Two sources, chosen app-wide by the admin (App Settings → model table → Advanced,
key ``embedding_source`` in data/config/app-settings.json):

  • "cloud"  (default) — call the SAME provider config as the chat agent
    (LLM_BASE_URL / LLM_API_KEY set from the web UI Settings modal). Model from
    EMBED_MODEL env / app-settings (default: text-embedding-3-small, 1536 dims).
  • "local"  — embed IN-PROCESS with a small quantised model via FastEmbed
    (ONNX Runtime, CPU, no GPU). No network hop: the query-embedding round-trip
    that gates every memory search disappears. Model weights load lazily on first
    use and stay resident (~a few hundred MB) for the life of the process.

The embedding DIMENSION depends on the active source/model. It is a single
app-wide value because every stored vector lives in one shared column and a
search only works if the query was embedded by the SAME model that embedded the
stored memories. Switching source/model therefore requires re-embedding every
stored chunk (the "reindex" migration in the DB backends) — until that runs,
old-width vectors are simply ignored and search falls back to keyword-only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

_embed_client = None
_embed_model_name: str = ""

# Default dimension for the CLOUD default model (text-embedding-3-small = 1536).
# Overridable via EMBED_DIM env for a different cloud model. The LOCAL source
# derives its dimension from the chosen model (see _LOCAL_MODEL_DIMS).
EMBED_DIM_DEFAULT = int(os.environ.get("EMBED_DIM") or 1536)

# Back-compat alias: some modules imported EMBED_DIM as a constant. It now equals
# the cloud default; callers that must honour a runtime source/model switch should
# call embed_dim() instead of reading this frozen value.
EMBED_DIM = EMBED_DIM_DEFAULT

# ── Local (in-process) model catalog ───────────────────────────────────────
# Small, CPU-friendly embedding models FastEmbed can serve. The value is the
# model's output width — the app needs it up front (before any vector exists) to
# size the pgvector column and to filter stored vectors by width during search.
# All are English text embedders; the 384-dim ones are the lightest (~<1GB RAM)
# and are plenty for memory recall.
_LOCAL_MODEL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "snowflake/snowflake-arctic-embed-s": 384,
}
_DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"

# ── App-settings read (mtime-cached) ────────────────────────────────────────
# embed_text runs on the hot path, so re-reading a JSON file every call would be
# wasteful; cache it and only re-parse when the file's mtime changes. This lets an
# admin flip the source in the UI and have it take effect on the next turn with NO
# server restart (the settings POST rewrites the file → mtime bumps → cache miss).
_APP_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data" / "config" / "app-settings.json"
_cfg_cache: dict = {"mtime": None, "data": {}}


def _app_cfg() -> dict:
    try:
        st = _APP_SETTINGS_FILE.stat()
    except OSError:
        return {}
    if _cfg_cache["mtime"] != st.st_mtime:
        try:
            with open(_APP_SETTINGS_FILE) as f:
                _cfg_cache["data"] = json.load(f) or {}
        except Exception as e:  # pragma: no cover
            logger.warning("embed: could not read app-settings.json: %s", e)
            _cfg_cache["data"] = {}
        _cfg_cache["mtime"] = st.st_mtime
    return _cfg_cache["data"]


def local_models() -> dict:
    """{model_id: output_dim} for the selectable in-process models (for the UI)."""
    return dict(_LOCAL_MODEL_DIMS)


def embed_source() -> str:
    """Active embedding source: "local" or "cloud". Env override wins (for tests
    / headless), else app-settings, else the app default (local — in-process,
    no per-call network hop, no embedding API cost). If the local dependency
    isn't installed, callers degrade to keyword-only search rather than crash;
    the chosen width stays deterministic (see embed_dim), so no vector is ever
    written at the wrong size."""
    src = (os.environ.get("EMBED_SOURCE") or _app_cfg().get("embedding_source") or "local")
    src = str(src).strip().lower()
    return "cloud" if src == "cloud" else "local"


def embed_model_name() -> str:
    """The model id for the active source."""
    if embed_source() == "local":
        m = (os.environ.get("EMBED_MODEL_LOCAL")
             or _app_cfg().get("embedding_model")
             or _DEFAULT_LOCAL_MODEL)
        m = str(m).strip()
        return m if m in _LOCAL_MODEL_DIMS else _DEFAULT_LOCAL_MODEL
    return os.environ.get("EMBED_MODEL") or _app_cfg().get("embedding_model_cloud") or "text-embedding-3-small"


def embed_dim() -> int:
    """Active embedding width. Drives the vector-column size and the search-time
    filter that ignores stored vectors of a different (pre-switch) width."""
    if embed_source() == "local":
        return _LOCAL_MODEL_DIMS.get(embed_model_name(), 384)
    # Cloud: a reindex records the active width in app-settings; fall back to the
    # env/default for the standard 1536-wide model.
    try:
        return int(_app_cfg().get("embedding_dim") or EMBED_DIM_DEFAULT)
    except (TypeError, ValueError):
        return EMBED_DIM_DEFAULT


# ── Timeouts / retries / circuit breaker (CLOUD path only) ──────────────────
# How long a single embedding request may take, and how many times the client
# retries. Default OpenAI retry is 2 with exponential backoff — fine for a
# working provider, but when embeddings are MISCONFIGURED (e.g. the chat provider
# is OpenRouter, which has no /embeddings endpoint) every memory search pays
# 15s + retries ≈ 25s before failing. Fail fast instead; the circuit breaker
# below then skips embeddings entirely for a cooldown so the stall never repeats.
_EMBED_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT") or 8.0)
_EMBED_RETRIES = int(os.environ.get("EMBED_RETRIES") or 0)

# The embeddings endpoint is either working or it isn't (wrong provider / no
# key). Rather than retry a known-dead endpoint on every turn — adding ~25s of
# "searching memory" each time — one failure OPENS the breaker for a cooldown,
# during which embed_text() raises instantly (no network) so callers fall back
# to keyword-only memory search with zero added latency. It self-heals: after the
# cooldown the next call probes once, and a success closes the breaker. The
# breaker applies to the CLOUD path only; the local path has no network to fail.
_BREAKER_COOLDOWN = float(os.environ.get("EMBED_BREAKER_COOLDOWN") or 300.0)  # 5 min
_breaker_until: float = 0.0  # time.monotonic() value; >now means "skip embeddings"


class EmbeddingsUnavailable(RuntimeError):
    """Raised immediately (no network call) while the breaker is open, so memory
    search/save skip the embedding round-trip instead of stalling on it."""


def embeddings_available() -> bool:
    """True if a call would be attempted right now. Local is always available
    once its dependency is importable; cloud depends on the breaker being closed."""
    if embed_source() == "local":
        return local_embeddings_available()[0]
    return time.monotonic() >= _breaker_until


# ── Local engine (FastEmbed / ONNX, in-process) ─────────────────────────────
_local_embedder = None
_local_embedder_model: str = ""


def local_embeddings_available() -> Tuple[bool, str]:
    """(ok, reason). ok=False with a human reason when FastEmbed can't be used —
    the UI surfaces this so an admin who picks "local" without the dependency
    installed sees why memory search silently fell back to keyword-only."""
    try:
        import fastembed  # noqa: F401
        return True, ""
    except Exception as e:  # pragma: no cover - env dependent
        return False, f"fastembed not installed ({e})"


def _get_local_embedder():
    """Lazy-init the in-process embedder. First call downloads (once) + loads the
    model; both are done inside a worker thread by the caller so the event loop
    is never blocked. The instance is cached and reused for the process lifetime,
    so subsequent embeds are just a forward pass."""
    global _local_embedder, _local_embedder_model
    want = embed_model_name()
    if _local_embedder is None or _local_embedder_model != want:
        from fastembed import TextEmbedding
        logger.info("Loading local embedding model: %s", want)
        _local_embedder = TextEmbedding(model_name=want)
        _local_embedder_model = want
    return _local_embedder


async def _embed_local(text: str) -> List[float]:
    import asyncio

    def _run() -> List[float]:
        embedder = _get_local_embedder()
        # FastEmbed returns a generator of numpy arrays, one per input string.
        vecs = list(embedder.embed([text[:8000]]))
        return [float(x) for x in vecs[0]]

    return await asyncio.to_thread(_run)


# ── Cloud engine (OpenAI-compatible, same provider as chat) ─────────────────
def _get_embed_client():
    """Lazy-init the cloud embed client, same provider config as chat.

    Reads LLM_BASE_URL / LLM_API_KEY (set by admin/settings.py on provider save),
    falls back to OPENROUTER_* env vars for dev convenience.
    """
    global _embed_client, _embed_model_name
    want = embed_model_name()
    if _embed_client is None or _embed_model_name != want:
        from openai import AsyncOpenAI

        base_url = (
            os.environ.get("LLM_BASE_URL")
            or os.environ.get("OPENROUTER_BASE_URL")
        )
        api_key = (
            os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or ""
        )

        _embed_client = AsyncOpenAI(
            base_url=base_url, api_key=api_key,
            timeout=_EMBED_TIMEOUT, max_retries=_EMBED_RETRIES,
        )
        _embed_model_name = want
        logger.info("Embed client initialised: model=%s base_url=%s", want, base_url)
    return _embed_client, _embed_model_name


async def _embed_cloud(text: str) -> List[float]:
    global _breaker_until
    # Breaker open → fail instantly so callers skip the embedding round-trip.
    if time.monotonic() < _breaker_until:
        raise EmbeddingsUnavailable("embeddings paused (circuit breaker open)")
    client, model = _get_embed_client()
    try:
        response = await client.embeddings.create(model=model, input=text[:8000])
    except Exception as e:
        # Trip the breaker: a misconfigured/unreachable endpoint shouldn't add
        # ~25s to every memory search. Skip embeddings for the cooldown window.
        _breaker_until = time.monotonic() + _BREAKER_COOLDOWN
        logger.warning(
            "Embedding call failed; pausing embeddings for %.0fs (keyword-only "
            "memory until then): %s", _BREAKER_COOLDOWN, e)
        raise
    # Success → close the breaker if it was open (provider recovered).
    if _breaker_until:
        _breaker_until = 0.0
    # Record background usage without blocking this latency-sensitive call:
    # memory recall awaits embeddings before the LLM turn, so fire-and-forget.
    try:
        _u = getattr(response, "usage", None)
        if _u:
            import asyncio
            from plugins.billing.usage import record_background_usage
            _tok = getattr(_u, "prompt_tokens", None)
            if _tok is None:
                _tok = getattr(_u, "total_tokens", 0) or 0
            asyncio.create_task(record_background_usage(
                model=model, input_tokens=_tok or 0, label="embed"))
    except Exception:
        pass
    return response.data[0].embedding


async def embed_text(text: str) -> List[float]:
    """Embed a single text string via the active source (local or cloud).

    Returns an ``embed_dim()``-wide list of floats. Raises on error (caller
    should catch and fall back gracefully — search → keyword-only, save → store
    the chunk without an embedding)."""
    if embed_source() == "local":
        return await _embed_local(text)
    return await _embed_cloud(text)


async def warm_embed_client() -> bool:
    """Best-effort warm-up, called once at startup, for whichever source is active.

    The first embedding of a process pays a cold-start penalty — for cloud it's
    the lazy openai import + client construction + DNS/TLS + provider cold
    routing (~7.5s); for local it's the model download (first ever) + ONNX load.
    Firing one throwaway embed at boot moves that cost off the user's first chat
    turn so the pre-agent memory_search no longer stalls visibly.

    Safe to call before anything is configured — it logs and returns False rather
    than raising. The real call still falls back lazily on first use.
    """
    src = embed_source()
    if src == "cloud" and not (os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
        logger.info("Embed warmup skipped: no provider API key configured yet")
        return False
    if src == "local":
        ok, reason = local_embeddings_available()
        if not ok:
            logger.warning("Embed warmup skipped: local source selected but %s", reason)
            return False
    t = time.perf_counter()
    try:
        await embed_text("warmup")
        logger.info("Embed client warmed in %.2fs (source=%s, model=%s)",
                    time.perf_counter() - t, src, embed_model_name())
        return True
    except Exception as e:
        logger.warning("Embed warmup failed (will retry lazily on first use): %s", e)
        return False
