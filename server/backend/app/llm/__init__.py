"""Pluggable LLM providers. Every provider implements `structured(system, prompt, schema, model, images)`."""
from ..config import get_settings
from .base import LLM, LLMError

_instance: LLM | None = None


def get_llm() -> LLM:
    global _instance
    if _instance is None:
        s = get_settings()
        if s.llm_provider == "cli":
            from .cli import ClaudeCLI
            _instance = ClaudeCLI()
        elif s.llm_provider == "anthropic":
            from .anthropic_api import AnthropicAPI
            _instance = AnthropicAPI()
        elif s.llm_provider == "mock":
            from .mock import MockLLM
            _instance = MockLLM()
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER {s.llm_provider!r}")
    return _instance


_copilot: LLM | None = None


def get_llm_for(provider: str) -> LLM:
    """The model a user picked: Claude is the server's configured provider, Copilot is its own CLI provider.
    In mock mode both are the mock, so demos and tests need neither."""
    global _copilot
    if provider == "copilot" and get_settings().llm_provider != "mock":
        if _copilot is None:
            from .copilot_cli import CopilotCLI
            _copilot = CopilotCLI()
        return _copilot
    return get_llm()


def set_copilot_llm(llm: LLM | None) -> None:
    """For tests."""
    global _copilot
    _copilot = llm


def set_llm(llm: LLM | None) -> None:
    """For tests."""
    global _instance
    _instance = llm


__all__ = ["LLM", "LLMError", "get_llm", "get_llm_for", "set_llm", "set_copilot_llm"]
