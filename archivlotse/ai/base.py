from __future__ import annotations

from dataclasses import dataclass

# Sampling temperature for every AI call, independent of provider/model.
# ArchivLotse only asks for extraction/classification answers (a document
# type, a date, an amount), where the same input should always give the
# same answer - a provider's default (often ~0.7) made e.g. an invoice
# amount flip between runs.
TEMPERATURE = 0.0


@dataclass
class AIResult:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
