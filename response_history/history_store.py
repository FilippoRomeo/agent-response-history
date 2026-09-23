"""Named, persistent snapshots of the visible conversation plus how to reopen the native session.

Data lives outside the installer-owned code directory; nothing here touches the clipboard.
"""
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from response_history.model import TranscriptError
from response_history.session import resolve
from response_history.transcript import load_turns, preview

NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
STORE_ARGS = re.compile(r'(\S+)(?:[ \t]+"(.*)")?')
CLIENTS = {"claude": "Claude Code", "codex": "Codex"}
LIST = "list"  # explicit list form: Codex's $-mention picker swallows Enter on a bare "$retrieve-history"


class HistoryError(ValueError):
    """A local refusal whose message is safe to show (never transcript content)."""


def store_root() -> Path:
    return Path.home() / ".local/share/agent-response-history-sessions"


def project_root(cwd: str) -> Path:
    path = Path(cwd).resolve()
    for folder in (path, *path.parents):
        if (folder / ".git").exists():  # file or directory: worktrees and submodules use a .git file
            return folder
    return path


def project_dir(root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return store_root() / f"{root.name or 'root'}--{digest}"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(src: Path, dst: Path) -> None:
    """Atomic rename that never replaces dst, not even an empty directory (os.rename would)."""
    try:
        renamex = ctypes.CDLL(None, use_errno=True).renamex_np  # macOS 10.12+
    except AttributeError:
        raise OSError(errno.ENOTSUP, "No atomic no-replace rename on this platform") from None
    if renamex(os.fsencode(src), os.fsencode(dst), 0x4) != 0:  # RENAME_EXCL
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise HistoryError("Invalid session name: use 1-64 characters from A-Z a-z 0-9 _ -")
    if name == LIST:
        raise HistoryError(f"'{LIST}' is reserved: retrieve-history {LIST} shows all stored sessions")
    return name


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render(name: str, turns) -> str:
    sections = [f"# Session: {name}"]
    for turn in turns:
        sections += ["## User", turn.prompt, "## Assistant", turn.text]
    return "\n\n".join(sections) + "\n"


def store(provider: str, session: str, transcript: Path, cwd: str, name: str, note: str = "", now: datetime | None = None) -> tuple[Path, int, bool]:
    """Returns (session folder, reply count, durable). Anything failing before the atomic rename
    raises and leaves no session; after it, only durability confirmation can fail."""
    _check_name(name)
    root = project_root(cwd)
    target_parent = project_dir(root)
    if (target_parent / name).exists():
        raise HistoryError(f"A stored session named {name} already exists in this project; nothing was stored.")
    turns = load_turns(provider, transcript, session) if transcript.exists() else []
    if not turns:
        raise HistoryError("No responses yet; nothing was stored.")
    meta = {
        "schema_version": 1,
        "name": name,
        "note": note,
        "provider": provider,
        "session_id": session,
        "project_path": str(root),
        "cwd": str(Path(cwd).resolve()),  # native resume lookup is keyed by launch directory
        "stored_at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
        "reply_count": len(turns),
        "first_prompt_preview": preview(turns[0].prompt),
        "last_reply_preview": preview(turns[-1].text),
    }
    store_root().parent.mkdir(parents=True, exist_ok=True)
    _private_dir(store_root())
    _private_dir(target_parent)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=target_parent))  # created 0700
    try:
        _write(tmp / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        _write(tmp / "replies.md", render(name, turns))
        _fsync_dir(tmp)
        try:
            _publish(tmp, target_parent / name)
        except FileExistsError:
            raise HistoryError(f"A stored session named {name} already exists in this project; nothing was stored.") from None
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        _fsync_dir(target_parent)
    except OSError:
        return target_parent / name, len(turns), False  # stored and visible; durability unconfirmed
    return target_parent / name, len(turns), True


def _load(folder: Path) -> dict:
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError
        return {**meta, "name": folder.name}  # the validated directory name is authoritative
    except (OSError, ValueError):
        return {"name": folder.name, "note": "(unreadable metadata)"}


def _stored(meta: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(meta.get("stored_at"))
    except (TypeError, ValueError):
        return None


def sessions(cwd: str) -> list[dict]:
    folder = project_dir(project_root(cwd))
    found = [_load(p) for p in sorted(folder.iterdir()) if p.is_dir() and not p.name.startswith(".")] if folder.is_dir() else []
    # Newest first, name breaks ties; undated (unreadable) entries last.
    return sorted(found, key=lambda m: (_stored(m) is None, -(_stored(m).timestamp() if _stored(m) else 0), str(m.get("name"))))


def _date(meta: dict) -> str:
    stamp = _stored(meta)
    return stamp.strftime("%d %b %Y") if stamp else "?"


def _summary(meta: dict) -> str:
    return preview(str(meta.get("note") or meta.get("first_prompt_preview") or ""))


def listing(cwd: str) -> str:
    found = sessions(cwd)
    if not found:
        return "No stored sessions for this project."
    width = max([19] + [len(str(m.get("name"))) for m in found])
    lines = [f"{'Name':<{width}}  Client   Replies   Stored       What it did",
             f"{'-' * width}  -------  --------  -----------  ----------------------------"]
    for meta in found:
        what = _summary(meta)
        if len(what) > 48:
            what = what[:47] + "…"
        client = {"claude": "Claude", "codex": "Codex"}.get(meta.get("provider"), "?")
        lines.append(f"{str(meta.get('name')):<{width}}  {client:<7}  {str(meta.get('reply_count', '?')):<8}  {_date(meta):<11}  {what}".rstrip())
    return "\n".join(lines)


def detail(cwd: str, name: str) -> str:
    _check_name(name)
    folder = project_dir(project_root(cwd)) / name
    if not folder.is_dir():
        raise HistoryError(f"No stored session named {name} in this project.")
    meta = _load(folder)
    provider, session, project = meta.get("provider"), meta.get("session_id"), meta.get("project_path")
    launch = meta.get("cwd") or project
    try:
        available = provider in CLIENTS and isinstance(launch, str) and bool(resolve(provider, session))
    except TranscriptError:
        available = False
    lines = [name, CLIENTS.get(provider, "Unknown client"), f"{meta.get('reply_count', '?')} replies",
             f"Stored: {_date(meta)}", f"Project: {project}"]
    if _summary(meta):
        lines += ["", _summary(meta)]
    lines += ["", f"Session: {'AVAILABLE' if available else 'MISSING'}"]
    if available:
        resume = "claude --resume" if provider == "claude" else "codex resume"
        lines += ["", "Reopen:", f"cd {_quote(launch)} && {resume} {_quote(session)}"]
    lines += ["", f"Archived replies: {folder / 'replies.md'}"]
    return "\n".join(lines)


def run(provider: str, command: str, args: str, session: str, transcript: str, cwd: str) -> str:
    """Execute one exact history command; returns the local reply text."""
    prefix = "/" if provider == "claude" else "$"
    try:
        if command == "store-history":
            match = STORE_ARGS.fullmatch(args)
            if match is None:
                raise HistoryError(f'Usage: {prefix}store-history NAME ["NOTE"]')
            path, count, durable = store(provider, session, Path(transcript), cwd, match.group(1), match.group(2) or "")
            message = f"Stored {match.group(1)} ({count} replies).\n{path}"
            if not durable:
                message += "\nWarning: the session was stored, but filesystem durability could not be confirmed."
            return message
        if args in ("", LIST):
            return listing(cwd)
        if len(args.split()) != 1:
            raise HistoryError(f"Usage: {prefix}retrieve-history [{LIST} | NAME]")
        return detail(cwd, args)
    except HistoryError as exc:
        return str(exc)
    except (OSError, UnicodeError, ValueError, TranscriptError) as exc:
        # Do not echo exception details: filenames and transcript bodies may be private.
        return f"Stored-session error: {type(exc).__name__}"
