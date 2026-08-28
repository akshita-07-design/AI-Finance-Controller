"""
A prompt->response cache, keyed by sha256(prompt).

Caching a prompt's response means the pipeline never pays for the same
question twice within a run — a real cost and latency win regardless of
model behavior. It is NOT, however, a guarantee that re-running the
pipeline reproduces bit-for-bit identical LLM decisions: as of Gemini 3.6
Flash, the sampling parameters (temperature/top_p/top_k) that would once
have made a call deterministic are deprecated and silently ignored by the
API itself, so nothing at the client-settings level can promise that same-
prompt-in implies same-response-out across separate calls. Within a single
run, though, this cache still means "asked once, never asked again" — which
is the property that actually matters for cost and for keeping repeated
adjudication of the same ambiguous group consistent within that run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class PromptCache:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, str] = {}
        if self.path.exists():
            with open(self.path) as f:
                self._data = json.load(f)

    def get(self, prompt_hash: str) -> str | None:
        return self._data.get(prompt_hash)

    def set(self, prompt_hash: str, response_text: str) -> None:
        self._data[prompt_hash] = response_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)
