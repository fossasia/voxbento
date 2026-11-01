from __future__ import annotations

from datetime import timedelta
from enum import Enum


class AnalysisProviderEnum(str, Enum):
    LOCAL = "local"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GROQ = "groq"
    OPENROUTER = "openrouter"


DEFAULT_MODELS: dict[str, str] = {
    AnalysisProviderEnum.LOCAL.value: "gemma3:4b",
    AnalysisProviderEnum.OPENAI.value: "gpt-4o-mini",
    AnalysisProviderEnum.ANTHROPIC.value: "claude-haiku-4-5-20251001",
    AnalysisProviderEnum.GEMINI.value: "gemini-3.8-flash",
    AnalysisProviderEnum.GROQ.value: "openai/gpt-oss-120b",
    AnalysisProviderEnum.OPENROUTER.value: "meta-llama/llama-3.3-70b-instruct",
}

PROVIDER_LABELS: dict[str, str] = {
    AnalysisProviderEnum.LOCAL.value: "Local (Ollama)",
    AnalysisProviderEnum.OPENAI.value: "OpenAI",
    AnalysisProviderEnum.ANTHROPIC.value: "Anthropic Claude",
    AnalysisProviderEnum.GEMINI.value: "Google Gemini",
    AnalysisProviderEnum.GROQ.value: "Groq",
    AnalysisProviderEnum.OPENROUTER.value: "OpenRouter",
}

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
IN_PROGRESS = (STATUS_PENDING, STATUS_RUNNING)

MAX_MODEL_LENGTH = 100
MAX_TRANSCRIPT_CHARS = 60_000
MAX_LIST_ITEMS = 20
MAX_DIGEST_CHUNKS = 30
REQUEST_TIMEOUT_SECONDS = 180.0
STALE_AFTER = timedelta(minutes=30)


def model_for(provider: str, model: str | None) -> str:
    return (model or "").strip() or DEFAULT_MODELS.get(provider, "")
