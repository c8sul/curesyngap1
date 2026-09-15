"""The system prompt, loaded from prompts/system.md."""

import os
from pathlib import Path

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"

FALLBACK_REPLY = (
    "Sorry, I'm having trouble answering right now. Please try again in a moment, "
    "or find what you need at https://curesyngap1.org/"
)

# Twilio rejects a WhatsApp or SMS body over 1600 characters, and it does so
# after accepting the send, so nothing reports the failure back and the family
# simply gets no reply. Staying under the limit with room to spare is the only
# way to guarantee they hear something.
MAX_REPLY_CHARS = 1500

# Sent once when a contact goes over the per-minute message limit. Further
# messages in the same window get no reply at all, so a loop on the other end
# cannot be answered message for message.
RATE_LIMITED_REPLY = (
    "You're sending messages faster than I can answer. Please wait a minute and "
    "try again, or find what you need at https://curesyngap1.org/"
)

# Sent in place of a reply too long to deliver. Worded for either cause: an
# off-topic request the model complied with, or an on-topic answer that ran long.
TOO_LONG_REPLY = (
    "Sorry, I can't help with that here. I can answer short questions about "
    "SYNGAP1 and CURE SYNGAP1. Please try a more specific question, or visit "
    "https://curesyngap1.org/"
)


def load_system_prompt(path: Path | None = None) -> str:
    """Read the system prompt.

    Content reviewers edit prompts/system.md directly; `PROMPT_PATH` overrides
    the location.
    """
    resolved = path or Path(os.environ.get("PROMPT_PATH", DEFAULT_PROMPT_PATH))
    return resolved.read_text().strip()
