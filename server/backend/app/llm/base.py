from typing import Protocol


class LLMError(Exception):
    """Raised with a message that is safe to show to the user."""


class LLM(Protocol):
    # True when each user must connect their own Claude login before analysis can run (the cli provider in per_user mode)
    needs_user_credentials: bool

    def structured(self, system: str, prompt: str, schema: dict, model: str, images: list[bytes] | None = None,
                   auth: str | None = None) -> dict:
        """Return a dict that conforms to `schema`. `auth` is the calling user's own Claude token, when the provider uses one."""
        ...
