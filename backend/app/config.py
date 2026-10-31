"""RESTORIO configuration.

Everything is environment-overridable so the demo can be re-pointed at a
different model provider without touching code. See .env.example.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env from the project root, once, before any setting is read.

    Every entry point -- the server, the test suites, the evaluation harness --
    goes through this module, so loading here means no script can silently run
    without the key. Real environment variables always win over the file.
    """
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        env = parent / ".env"
        if env.is_file():
            try:
                for line in env.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            except OSError:
                pass
            return


_load_dotenv()


def _raw(key: str) -> str | None:
    """Read a setting, preferring the RESTORIO_ prefix.

    The project was renamed from RESTORIO; the old prefix still resolves so an
    existing .env does not have to be rewritten.
    """
    if key in os.environ:
        return os.environ[key]
    if key.startswith("RESTORIO_"):
        legacy = "RESTORE_" + key[len("RESTORIO_"):]   # pre-rename prefix
        if legacy in os.environ:
            return os.environ[legacy]
    return None


def _s(key: str, default: str) -> str:
    v = _raw(key)
    return default if v is None else v


def _f(key: str, default: float) -> float:
    try:
        v = _raw(key)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _i(key: str, default: int) -> int:
    try:
        v = _raw(key)
        return default if v is None else int(v)
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool) -> bool:
    raw = _raw(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # ------------------------------------------------------------------
    # Model provider.  'auto' picks the first provider that has credentials,
    # falling back to the deterministic offline brain so the demo always runs.
    # ------------------------------------------------------------------
    provider: str = field(default_factory=lambda: _s("RESTORIO_PROVIDER", "auto"))
    anthropic_api_key: str = field(default_factory=lambda: _s("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: _s("OPENAI_API_KEY", ""))
    # Any OpenAI-compatible endpoint: Kimi/Moonshot, DeepSeek, Groq, Together,
    # OpenRouter, vLLM. Only the base URL and model name change.
    openai_base_url: str = field(
        default_factory=lambda: _s("RESTORIO_OPENAI_BASE_URL",
                                   "https://api.openai.com/v1"))
    kimi_api_key: str = field(
        default_factory=lambda: _s("KIMI_API_KEY", "") or _s("MOONSHOT_API_KEY", ""))
    kimi_base_url: str = field(
        default_factory=lambda: _s("RESTORIO_KIMI_BASE_URL",
                                   "https://api.moonshot.ai/v1"))
    kimi_model: str = field(
        default_factory=lambda: _s("RESTORIO_KIMI_MODEL", "kimi-k2-0711-preview"))
    ollama_host: str = field(default_factory=lambda: _s("OLLAMA_HOST", "http://localhost:11434"))

    model_deep: str = field(
        default_factory=lambda: _s("RESTORIO_MODEL", "claude-sonnet-5-5"))
    # If the primary model declines a request, retry once here. Anthropic's own
    # guidance for integrators is to configure a fallback rather than let a
    # refusal end the run.
    model_fallback: str = field(
        default_factory=lambda: _s("RESTORIO_MODEL_FALLBACK", "claude-haiku-5-5"))
    openai_model: str = field(default_factory=lambda: _s("RESTORIO_OPENAI_MODEL", "gpt-4o"))
    ollama_model: str = field(default_factory=lambda: _s("RESTORIO_OLLAMA_MODEL", "llama3.1"))

    max_tokens: int = field(default_factory=lambda: _i("RESTORIO_MAX_TOKENS", 4096))
    llm_timeout_s: float = field(default_factory=lambda: _f("RESTORIO_LLM_TIMEOUT", 120.0))
    llm_retries: int = field(default_factory=lambda: _i("RESTORIO_LLM_RETRIES", 5))
    # Backoff floor between retries. A provider capped at N requests per minute
    # needs about 60/N seconds of spacing; raise this to match your tier.
    llm_retry_base_s: float = field(
        default_factory=lambda: _f("RESTORIO_LLM_RETRY_BASE", 8.0))
    llm_retry_max_s: float = field(
        default_factory=lambda: _f("RESTORIO_LLM_RETRY_MAX", 45.0))
    # Minimum spacing between model calls, applied proactively. Set this from
    # your rate limit (3 requests/min -> 21) to avoid 429s altogether.
    llm_min_interval_s: float = field(
        default_factory=lambda: _f("RESTORIO_LLM_MIN_INTERVAL", 0.0))

    # ------------------------------------------------------------------
    # Process model.  Values mirror the project specification, section 03/04.
    # ------------------------------------------------------------------
    tick_hz: float = field(default_factory=lambda: _f("RESTORIO_TICK_HZ", 5.0))
    setpoint_bar: float = field(default_factory=lambda: _f("RESTORIO_SETPOINT", 4.0))
    target_band_bar: float = field(default_factory=lambda: _f("RESTORIO_TARGET_BAND", 0.3))
    agreement_tol_bar: float = field(default_factory=lambda: _f("RESTORIO_AGREEMENT_TOL", 0.2))
    settle_seconds: float = field(default_factory=lambda: _f("RESTORIO_SETTLE_SECONDS", 10.0))
    # "Verify: confirm recovery and monitor". After the checks pass the station
    # is watched for this long before an incident may be closed as recovered.
    monitor_seconds: float = field(default_factory=lambda: _f("RESTORIO_MONITOR_SECONDS", 15.0))
    noise_bar: float = field(default_factory=lambda: _f("RESTORIO_NOISE", 0.012))
    seed: int = field(default_factory=lambda: _i("RESTORIO_SEED", 20261030))

    # Hard protection limit -- above this the simulator latches a safe state
    # regardless of what the controller or the agent want.
    overpressure_trip_bar: float = field(default_factory=lambda: _f("RESTORIO_TRIP_BAR", 9.0))

    # ------------------------------------------------------------------
    # Agent guardrails.
    # ------------------------------------------------------------------
    # The agent may not apply a response it is less sure of than this.
    min_confidence_to_act: float = field(default_factory=lambda: _f("RESTORIO_MIN_CONFIDENCE", 0.6))
    # Bounded investigation.  Prevents the "retries forever" failure mode.
    # A multi-fault incident legitimately needs three investigate -> test ->
    # apply -> verify cycles, so the ceiling has to clear that with margin.
    # It is still a hard bound: hitting it escalates, it never retries forever.
    max_tool_calls: int = field(default_factory=lambda: _i("RESTORIO_MAX_TOOL_CALLS", 70))
    max_iterations: int = field(default_factory=lambda: _i("RESTORIO_MAX_ITERATIONS", 34))
    max_apply_attempts: int = field(default_factory=lambda: _i("RESTORIO_MAX_APPLY", 3))
    incident_budget_s: float = field(default_factory=lambda: _f("RESTORIO_INCIDENT_BUDGET", 300.0))
    # supervised: every apply needs a human click.  guarded: only high risk.
    # full: no approval gate (used by the evaluation harness).
    autonomy: str = field(default_factory=lambda: _s("RESTORIO_AUTONOMY", "guarded"))
    # In 'guarded' mode, actions at or above this tier need a human click.
    approval_risk_threshold: str = field(
        default_factory=lambda: _s("RESTORIO_APPROVAL_TIER", "high"))
    # Wall-clock speed multiplier for the simulation loop. 1.0 = real time.
    speed: float = field(default_factory=lambda: _f("RESTORIO_SPEED", 1.0))

    # ------------------------------------------------------------------
    # Ablation switches. EVALUATION ONLY -- every one of these disables a
    # safety property, and all are empty in normal operation. Used by
    # scripts/ablate.py to show that each guardrail blocks something real.
    # ------------------------------------------------------------------
    ablate: set = field(default_factory=set)

    # ------------------------------------------------------------------
    # Detection.
    # ------------------------------------------------------------------
    detect_disagreement_bar: float = field(default_factory=lambda: _f("RESTORIO_DETECT_BAR", 0.45))
    detect_sustain_s: float = field(default_factory=lambda: _f("RESTORIO_DETECT_SUSTAIN", 3.0))
    staleness_limit_s: float = field(default_factory=lambda: _f("RESTORIO_STALENESS", 5.0))

    # ------------------------------------------------------------------
    # Server / storage.
    # ------------------------------------------------------------------
    host: str = field(default_factory=lambda: _s("RESTORIO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _i("RESTORIO_PORT", 8080))
    db_path: str = field(default_factory=lambda: _s("RESTORIO_DB", "restorio_audit.db"))
    serve_frontend: bool = field(default_factory=lambda: _b("RESTORIO_SERVE_FRONTEND", True))

    def resolved_provider(self) -> str:
        if self.provider != "auto":
            return self.provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.kimi_api_key:
            return "kimi"
        if self.openai_api_key:
            return "openai"
        return "offline"

    @property
    def dt(self) -> float:
        return 1.0 / self.tick_hz


settings = Settings()
