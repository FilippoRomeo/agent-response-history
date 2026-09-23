"""Load one transcript into complete, selectable turns (shared by copy/list and stored sessions)."""
import unicodedata
from pathlib import Path
from response_history.adapters import codex, claude
from response_history.adapters.common import records
from response_history.model import TranscriptError


def preview(text: str) -> str:
    safe = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    return " ".join(safe.split())[:120]


def load_turns(provider: str, path: Path, session: str | None = None) -> list:
    """Parse `path`; with `session`, refuse a transcript that belongs to another session."""
    rows = records(path)
    if provider == "auto":
        kinds = {r.get("type") for r in rows}
        codex_shape = bool(kinds & {"session_meta", "event_msg", "response_item"})
        claude_shape = bool(kinds & {"user", "assistant", "bridge-session"})
        if codex_shape == claude_shape:
            raise TranscriptError("Ambiguous transcript provider")
        provider = "codex" if codex_shape else "claude"
    if session and rows:  # an empty transcript has no identity and nothing to attribute
        ids = ({r.get("payload", {}).get("id") for r in rows if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict)}
               if provider == "codex" else {r.get("sessionId") for r in rows if r.get("sessionId")})
        if ids != {session}:
            raise TranscriptError("Session identity mismatch")
    return [t for t in (codex if provider == "codex" else claude).parse(rows, str(path)) if t.selectable]
