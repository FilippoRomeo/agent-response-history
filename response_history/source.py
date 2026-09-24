"""Where ls/copy get their responses from: the live transcript, or a stored session selected
for this one native Claude/Codex session.

The choice is kept per provider + native session ID, so selecting a stored session in one
session never changes what another session sees. No state file means LIVE.
"""
import json
import os
import tempfile
import uuid
from pathlib import Path
from response_history import archive
from response_history.model import UserError
from response_history.transcript import load_turns

GONE = "The selected stored session is no longer available ({}). Choose another stored session or switch back to the live session."


def state_root() -> Path:
    return Path.home() / ".local/state/agent-response-history/sources"


def _state_file(provider: str, session: str | None) -> Path | None:
    if provider not in ("claude", "codex") or not session:
        return None
    try:
        return state_root() / provider / f"{uuid.UUID(session)}.json"  # a canonical UUID is a safe filename
    except (ValueError, AttributeError, TypeError):
        return None


def selected(provider: str, session: str | None) -> dict | None:
    """The stored session selected for this native session, or None for LIVE."""
    path = _state_file(provider, session)
    if path is None or not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("kind") != "archive" or not isinstance(state.get("path"), str):
            raise ValueError
        return state
    except (OSError, UnicodeError, ValueError):
        raise UserError(GONE.format("the selection record can't be read")) from None


def select_archive(provider: str, session: str, folder: Path) -> tuple[list, dict]:
    """Validate the stored session, then make it the source for this native session."""
    path = _state_file(provider, session)
    if path is None:
        raise UserError("This session can't select a stored session (unknown client or session).")
    turns, meta = archive.load(folder)  # refuses before anything is recorded
    for directory in (state_root().parent, state_root(), path.parent):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"kind": "archive", "path": str(Path(folder).resolve()), "name": Path(folder).resolve().name}, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return turns, meta


def select_live(provider: str, session: str) -> None:
    path = _state_file(provider, session)
    if path is not None:
        path.unlink(missing_ok=True)


def load(provider: str, session: str | None, transcript: Path) -> tuple[list, str | None]:
    """(turns, stored-session name or None for LIVE); a selected archive that won't load is refused."""
    state = selected(provider, session)
    if state is None:
        return load_turns(provider, transcript, session), None
    try:
        turns, meta = archive.load(Path(state["path"]))
    except UserError as exc:
        raise UserError(GONE.format(exc)) from None
    return turns, Path(state["path"]).name  # the folder's current name, even if it was renamed after storing
