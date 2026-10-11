from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AIResult:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
