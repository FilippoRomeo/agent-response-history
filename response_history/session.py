"""Resolve one provider session to one local transcript, without recency guesses."""
import os
import uuid
from pathlib import Path
from response_history.model import TranscriptError


def resolve(provider: str, session: str) -> Path:
    try:
        session = str(uuid.UUID(session))
    except (ValueError, AttributeError):
        raise TranscriptError("Session must be a full UUID") from None
    if provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        matches = [p for folder in ("sessions", "archived_sessions")
                   for p in (root / folder).rglob(f"*{session}.jsonl")]
    elif provider == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        matches = list((root / "projects").rglob(f"{session}.jsonl"))
    else:
        raise TranscriptError("A provider is required for session lookup")
    if len(matches) != 1:
        raise TranscriptError("Session transcript missing or ambiguous")
    return matches[0]
