"""Resolve one provider session to one local transcript, without recency guesses."""
import json
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


def _json_lines(path: Path, marker: str):
    """Records from a JSONL file that mention marker; unreadable lines are skipped."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if marker in line:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
    except OSError:
        return


def title(provider: str, session: str, transcript: Path | None) -> tuple[str | None, str | None]:
    """The client's own name for this session and where it came from, or (None, None).

    Claude: the last /rename (custom-title) unless it cleared the name, else the last automatic ai-title, both
    read from the session transcript. Codex: the last thread_name for this thread in
    $CODEX_HOME/session_index.jsonl, which holds both /rename and generated names (verified
    with Codex 0.156.1); threads.title is the first prompt, not a title, and is never used.
    """
    if provider == "claude" and transcript is not None:
        found = {"custom-title": None, "ai-title": None}
        for record in _json_lines(transcript, "-title"):
            kind = record.get("type")
            value = record.get("customTitle" if kind == "custom-title" else "aiTitle")
            if kind == "custom-title" and isinstance(value, str):
                found[kind] = value  # the last record wins: an empty one clears the name, as in Claude Code
            elif kind == "ai-title" and isinstance(value, str) and value.strip():
                found[kind] = value
        for kind in ("custom-title", "ai-title"):
            if found[kind] and found[kind].strip():
                return found[kind], kind
    elif provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        name = None
        for record in _json_lines(root / "session_index.jsonl", session):
            value = record.get("thread_name")
            if record.get("id") == session and isinstance(value, str) and value.strip():
                name = value
        if name:
            return name, "codex-thread-name"
    return None, None
