"""prompt.py — Prompt building, sanitization, and ChatML formatting for SLM pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata
from typing import Protocol

PROMPT_VERSION = "p1"


def sanitize(text: str) -> str:
    """Sanitize inbound utterance text before inserting into LLM prompt.

    Rules:
    1. Unicode NFKC normalization.
    2. Remove control characters and the sequences `<|` and `|>`.
    3. Replace `"` with `'`. Replace newlines and tabs with a single space. Collapse whitespace.
    4. Truncate to 200 characters.
    5. Returns stripped sanitized string.
    """
    if not text:
        return ""

    # 1. Unicode NFKC normalization
    normalized = unicodedata.normalize("NFKC", text)

    # 2. Strip special tokens <| and |>
    cleaned = normalized.replace("<|", "").replace("|>", "")

    # Remove control characters (category C)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.category(ch).startswith("C"))

    # 3. Replace quotes and whitespace
    cleaned = cleaned.replace('"', "'")
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # 4. Truncate to 200 characters
    return cleaned[:200]


@dataclass(frozen=True)
class BuiltPrompt:
    text: str
    prefix_chars: int
    prefix_sha: str


class PromptTemplate(Protocol):
    version: str

    def build(self, sanitized_text: str, state: str) -> BuiltPrompt:
        ...


class PromptBuilder:
    """Qwen3 ChatML prompt builder with empty think block and cached static prefix."""

    def __init__(self, static_system_prompt: str, version: str = PROMPT_VERSION) -> None:
        self.version = version
        self._system_prompt = static_system_prompt.strip()

        # Static prefix ends before dynamic user inputs
        self._static_prefix = (
            f"<|im_start|>system\n"
            f"{self._system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n"
        )
        self._prefix_chars = len(self._static_prefix)
        self._prefix_sha = hashlib.sha256(self._static_prefix.encode("utf-8")).hexdigest()[:16]

    @property
    def static_prefix(self) -> str:
        return self._static_prefix

    @property
    def prefix_chars(self) -> int:
        return self._prefix_chars

    @property
    def prefix_sha(self) -> str:
        return self._prefix_sha

    def build(self, sanitized_text: str, state: str) -> BuiltPrompt:
        """Construct full prompt adhering to Qwen3 ChatML no-think template."""
        prompt_text = (
            f"{self._static_prefix}"
            f'Heard: "{sanitized_text}"\n'
            f"State: {state.upper()}<|im_end|>\n"
            f"<|im_start|>assistant\n"
            f"<think>\n\n</think>\n\n"
        )
        return BuiltPrompt(
            text=prompt_text,
            prefix_chars=self._prefix_chars,
            prefix_sha=self._prefix_sha,
        )
