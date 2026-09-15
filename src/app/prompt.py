"""The system prompt, loaded from prompts/system.md."""

import os
from pathlib import Path

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"

FALLBACK_REPLY = (
    "Sorry, I'm having trouble answering right now. Please try again in a moment, "
    "or find what you need at https://curesyngap1.org/"
)


def load_system_prompt(path: Path | None = None) -> str:
    """Read the system prompt.

    Content reviewers edit prompts/system.md directly; `PROMPT_PATH` overrides
    the location.
    """
    resolved = path or Path(os.environ.get("PROMPT_PATH", DEFAULT_PROMPT_PATH))
    return resolved.read_text().strip()
