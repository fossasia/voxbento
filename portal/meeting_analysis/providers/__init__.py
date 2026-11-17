from __future__ import annotations

from portal.meeting_analysis.constants import AnalysisProviderEnum
from portal.meeting_analysis.providers.anthropic import AnthropicProvider
from portal.meeting_analysis.providers.base import AnalysisProvider, Completion
from portal.meeting_analysis.providers.gemini import GeminiProvider
from portal.meeting_analysis.providers.local import LocalProvider
from portal.meeting_analysis.providers.openai import OpenAICompatibleProvider

__all__ = ["AnalysisProvider", "Completion", "get_provider"]


def get_provider(name: str) -> AnalysisProvider:
    provider = AnalysisProviderEnum(name)
    if provider is AnalysisProviderEnum.LOCAL:
        return LocalProvider()
    if provider is AnalysisProviderEnum.ANTHROPIC:
        return AnthropicProvider()
    if provider is AnalysisProviderEnum.GEMINI:
        return GeminiProvider()
    return OpenAICompatibleProvider(provider.value)
