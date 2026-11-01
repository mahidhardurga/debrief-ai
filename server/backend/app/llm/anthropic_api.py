"""Optional provider that calls the Anthropic Messages API with an API key (forced tool use for structure)."""
import base64
import json
import urllib.error
import urllib.request

from ..config import get_settings
from .base import LLMError

MODEL_ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}


class AnthropicAPI:
    needs_user_credentials = False        # one server-side API key serves everyone

    def __init__(self):
        s = get_settings()
        if not s.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set (LLM_PROVIDER=anthropic).")
        self.key, self.timeout = s.anthropic_api_key, s.llm_timeout

    def structured(self, system, prompt, schema, model, images=None, auth=None):
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(i).decode()}}
            for i in images or []]
        body = {"model": MODEL_ALIASES.get(model, model), "max_tokens": 8000, "system": system,
                "messages": [{"role": "user", "content": content}],
                "tools": [{"name": "report", "description": "Return the structured result", "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": "report"}}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-api-key": self.key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                j = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"Anthropic API error {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}")
        for c in j.get("content", []):
            if c.get("type") == "tool_use":
                return c["input"]
        raise LLMError("Claude did not return structured data.")
