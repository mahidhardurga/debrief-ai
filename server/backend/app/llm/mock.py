"""Deterministic fake LLM for tests, demos and CI: fills any JSON schema with plausible placeholder data."""


class MockLLM:
    needs_user_credentials = False

    def __init__(self):
        self.calls: list[dict] = []

    def structured(self, system, prompt, schema, model, images=None, auth=None):
        self.calls.append({"model": model, "images": len(images or []), "prompt_len": len(prompt), "auth": auth})
        n_lines = prompt.count("\n[")
        return self._gen(schema, "", n_lines)

    def _gen(self, sch, key, n):
        t = sch.get("type")
        if isinstance(t, list):
            t = [x for x in t if x != "null"][0]
        if "enum" in sch:
            return sch["enum"][0] if key == "decision" else sch["enum"][min(1, len(sch["enum"]) - 1)]
        if t == "object":
            return {k: self._gen(v, k, n) for k, v in sch["properties"].items()}
        if t == "array":
            if key == "areas":
                return []
            return [self._gen(sch["items"], key, n)] if n else []
        if t == "boolean":
            return True
        if t in ("integer", "number"):
            return 3
        return f"Mock {key or 'text'} ({n} lines)"
