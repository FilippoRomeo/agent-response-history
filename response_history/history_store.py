"""Named, persistent snapshots of the visible conversation plus how to reopen the native session.

Data lives outside the installer-owned code directory; nothing here touches the clipboard.
"""
import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from response_history.model import TranscriptError
from response_history.session import resolve, title as native_title
from response_history.transcript import load_turns, preview
from response_history import archive, source
from response_history.model import UserError

NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
CLIENTS = {"claude": "Claude Code", "codex": "Codex"}
LIST = "list"  # explicit list form: Codex's $-mention picker swallows Enter on a bare "$history-use"
LIVE = "live"
RESERVED = (LIST, LIVE)  # history-use keywords: no archive may be named like them, in any case
STORE_DIR = ".agent-response-history"  # the store in a folder: <cwd>/.agent-response-history/<name>/
HOME_STORE = "agent-response-history"  # the store inside a client's own config folder
SLUG_MAX = 48


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
    if name.lower() in RESERVED:
        raise HistoryError(f"'{name}' is reserved ({LIST} and {LIVE} are history-use keywords); choose another name.")
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
        "schema_version": archive.SCHEMA,
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
    try:
        return _publish_archive(target_parent, [name], meta, turns)
    except FileExistsError:
        raise HistoryError(f"A stored session named {name} already exists in this project; nothing was stored.") from None


def _publish_archive(parent: Path, names: list, meta: dict, turns, ignore_file: bool = False) -> tuple[Path, int, bool]:
    """Write the archive in a private temporary folder, then publish it under the first of `names`
    that is still free, with a rename that never replaces anything. FileExistsError when none is.
    Returns (folder, reply count, durable)."""
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=parent))  # created 0700
    try:
        _write(tmp / archive.TURNS, archive.dumps(turns))  # published together: the files can never disagree
        if ignore_file:
            _write(tmp / ".gitignore", "*\n")  # the archive ignores itself: never ordinary Git content
        for name in names:
            for stale in ("meta.json", "replies.md"):
                (tmp / stale).unlink(missing_ok=True)
            _write(tmp / "meta.json", json.dumps({**meta, "name": name}, ensure_ascii=False, indent=2) + "\n")
            _write(tmp / "replies.md", render(name, turns))
            _fsync_dir(tmp)
            try:
                _publish(tmp, parent / name)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        _fsync_dir(parent)
    except OSError:
        return parent / name, len(turns), False  # stored and visible; durability unconfirmed
    return parent / name, len(turns), True


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


def _date(meta: dict) -> str:
    stamp = _stored(meta)
    return stamp.strftime("%d %b %Y") if stamp else "?"


def _reopen(meta: dict) -> str | None:
    """The native resume command, only when the original session is still on this machine."""
    provider, session = meta.get("provider"), meta.get("session_id")
    launch = meta.get("cwd") or meta.get("project_path")
    try:
        if provider not in CLIENTS or not isinstance(launch, str) or not resolve(provider, session):
            return None
    except TranscriptError:
        return None
    resume = "claude --resume" if provider == "claude" else "codex resume"
    return f"cd {_quote(launch)} && {resume} {_quote(session)}"


def _current(provider: str, session: str) -> str:
    try:
        state = source.selected(provider, session)
    except UserError as exc:
        return f"Current source: unavailable. {exc}"
    if state is None:
        return "Current source: LIVE"
    name = Path(state["path"]).name
    try:
        archive.load(Path(state["path"]))
    except UserError as exc:
        return f"Current source: stored session {name}, no longer usable ({exc})"
    return f"Current source: stored session {name}"


# ---- where archives live -----------------------------------------------------------------

def here_root(cwd: str) -> Path:
    return Path(cwd).resolve() / STORE_DIR


def home_root(provider: str) -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") if provider == "claude" else os.environ.get("CODEX_HOME")
    return Path(base or Path.home() / (".claude" if provider == "claude" else ".codex")) / HOME_STORE


def stores(provider: str, cwd: str) -> list:
    """The stores history-use searches by name: this exact folder, this client's home store and
    this project's v2.1 store. Stores at explicit paths are reached by path only (no registry)."""
    return [("here", here_root(cwd)), ("home", home_root(provider)), ("v2.1", project_dir(project_root(cwd)))]


def discovered(provider: str, cwd: str) -> list:
    """(where, folder) for every archive folder in the discovered stores."""
    found, seen = [], set()
    for where, root in stores(provider, cwd):
        try:
            children = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            children = []
        for folder in children:
            if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir() or folder.resolve() in seen:
                continue
            seen.add(folder.resolve())
            found.append((where, folder))
    return found


def _user_path(text: str, cwd: str) -> Path:
    """A path typed by the person: ~ and ~/ expand, relative paths start at the session's folder."""
    if text == "~" or text.startswith("~/"):
        path = Path.home() / text[2:]
    elif text.startswith("~"):
        raise HistoryError(f"{text.split('/')[0]} paths aren't supported; write the full path.")
    else:
        path = Path(text)
    return path if path.is_absolute() else Path(cwd) / path


def _is_path(text: str) -> bool:
    return "/" in text or text.startswith(("~", "."))


def _check_root(root: Path, what: str) -> None:
    """A store folder must be a folder, or be creatable as one level under an existing folder."""
    if root.is_dir():
        return
    if os.path.lexists(root):
        raise HistoryError(f"Nothing stored: {what} is not a folder.")
    if not root.parent.is_dir():
        raise HistoryError(f"Nothing stored: the folder containing {what} doesn't exist.")


def _open_root(root: Path) -> Path:
    try:
        root.mkdir(mode=0o700)  # one level only, never parents
        os.chmod(root, 0o700)
    except FileExistsError:
        pass
    return root.resolve()


# ---- names ---------------------------------------------------------------------------------

def slug(text: str) -> str:
    """Lowercase ASCII folder name from a title; empty when nothing usable is left."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > SLUG_MAX:
        cut = text[:SLUG_MAX + 1].rfind("-")  # end at a word when that keeps most of the length
        text = text[:cut] if cut >= SLUG_MAX // 2 else text[:SLUG_MAX]
    return text.strip("-")


def _display(text: str) -> str:
    return " ".join(text.split())[:200]


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # never print or exit: the hook turns this into a usage reply
        raise _Usage(message)


def _store_args(args: str, prefix: str):
    usage = f"Nothing stored. Usage: {prefix}history-store [home | PATH] [--note TEXT] [--name NAME]"
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("where", nargs="?")
    parser.add_argument("--note", default="")
    parser.add_argument("--name")
    try:
        parsed = parser.parse_args(shlex.split(args))
    except (_Usage, ValueError):  # ValueError: unbalanced quotes
        raise HistoryError(usage) from None
    if parsed.where == "":
        raise HistoryError(usage)
    return parsed


def store_session(provider: str, session: str, transcript: Path, cwd: str, where: str | None = None,
                  name: str | None = None, note: str = "", now: datetime | None = None) -> tuple[Path, int, bool, dict]:
    """history-store: (archive folder, reply count, durable, meta). Refuses before writing anything."""
    if name is not None:
        _check_name(name)
    if where is None:
        root, what = here_root(cwd), f"{STORE_DIR} in this folder"
    elif where.lower() == "home":
        root, what = home_root(provider), f"{CLIENTS[provider]}'s home store"
    else:
        root = _user_path(where, cwd)
        what = str(root)
    _check_root(root, what)
    turns = load_turns(provider, transcript, session) if transcript.exists() else []
    if not turns:
        raise HistoryError("No responses yet; nothing was stored.")
    native, source_kind = native_title(provider, session, transcript)
    try:
        existing = [p.name for p in root.iterdir()] if root.is_dir() else []
    except OSError:
        existing = []
    taken = {n.lower() for n in existing} | {folder.name.lower() for _, folder in discovered(provider, cwd)}
    now = (now or datetime.now()).astimezone()
    if name is not None:
        if name.lower() in taken:
            raise HistoryError(f"Nothing stored: a stored session named {name} already exists; choose another --name.")
        names, name_source = [name], "explicit"
    else:
        base = slug(native) if native else ""
        name_source = "title" if base else "fallback"
        base = base or f"{provider}-{now:%Y-%m-%d}-{session[:8]}"
        names = [n for n in [base] + [f"{base}-{i}" for i in range(2, 100)] if n.lower() not in taken and n not in RESERVED]
    meta = {
        "schema_version": archive.SCHEMA,
        "name": names[0] if names else None,
        "title": _display(native) if native else None,  # the client's own name, exactly readable
        "title_source": source_kind,  # custom-title | ai-title | codex-thread-name | null
        "name_source": name_source,  # explicit | title | fallback
        "note": note,
        "provider": provider,
        "session_id": session,
        "cwd": str(Path(cwd).resolve()),  # native resume lookup is keyed by launch directory
        "stored_at": now.isoformat(timespec="seconds"),
        "reply_count": len(turns),
        "first_prompt_preview": preview(turns[0].prompt),
        "last_reply_preview": preview(turns[-1].text),
    }
    if not names:
        raise HistoryError("Nothing stored: too many stored sessions share this name; use --name.")
    root = _open_root(root)
    try:
        folder, count, durable = _publish_archive(root, names, meta, turns, ignore_file=True)
    except FileExistsError:  # claimed by another writer after every check
        raise HistoryError(f"Nothing stored: a stored session named {names[-1] if name else names[0]} already exists.") from None
    return folder, count, durable, {**meta, "name": folder.name}


def _stored_reply(provider: str, cwd: str, folder: Path, count: int, durable: bool, meta: dict) -> str:
    prefix = "/" if provider == "claude" else "$"
    name = folder.name
    if meta["name_source"] == "title":
        first = f'Stored {name} ({count} replies), named after "{meta["title"]}".'
    elif meta["name_source"] == "fallback":
        first = f"Stored {name} ({count} replies); this session has no title yet."
    else:
        first = f"Stored {name} ({count} replies)."
    root = folder.parent
    if root == home_root(provider).resolve():
        later = f"{prefix}history-use {name}"
    elif root == here_root(cwd):
        later = f"{prefix}history-use {name} (in this folder), or from anywhere: {prefix}history-use {_quote(str(folder))}"
    else:
        later = f"{prefix}history-use {_quote(str(folder))}"
    lines = [first, str(folder), f"Use it later with: {later}"]
    if not durable:
        lines.append("Warning: the session was stored, but filesystem durability could not be confirmed.")
    return "\n".join(lines)


# ---- history-use -------------------------------------------------------------------------

def _what(meta: dict) -> str:
    text = preview(str(meta.get("note") or meta.get("title") or meta.get("first_prompt_preview") or ""))
    return ("[v2.1 format] " + text) if meta.get("schema_version") == 1 else text


def use_listing(provider: str, cwd: str) -> str:
    found = [(where, _load(folder)) for where, folder in discovered(provider, cwd)]
    if not found:
        return f"No stored sessions in this folder, in {CLIENTS[provider]}'s home store or in this project's v2.1 store."
    order = {"here": 0, "home": 1, "v2.1": 2}
    found.sort(key=lambda e: (_stored(e[1]) is None, -(_stored(e[1]).timestamp() if _stored(e[1]) else 0), order[e[0]], str(e[1]["name"])))
    width = max([19] + [len(str(m["name"])) for _, m in found])
    lines = [f"{'Name':<{width}}  Where  Client   Replies   Stored       What it did",
             f"{'-' * width}  -----  -------  --------  -----------  ----------------------------"]
    for where, meta in found:
        what = _what(meta)
        if len(what) > 48:
            what = what[:47] + "…"
        client = {"claude": "Claude", "codex": "Codex"}.get(meta.get("provider"), "?")
        lines.append(f"{str(meta['name']):<{width}}  {where:<5}  {client:<7}  {str(meta.get('reply_count', '?')):<8}  {_date(meta):<11}  {what}".rstrip())
    return "\n".join(lines)


def use(provider: str, args: str, session: str, cwd: str) -> str:
    """history-use list | NAME | PATH | live: choose where history-list and history-copy read from.
    Never resumes, opens a terminal or sends anything to the model."""
    prefix = "/" if provider == "claude" else "$"
    usage = f"Nothing selected. Usage: {prefix}history-use {LIST} | NAME | PATH | {LIVE}"
    try:
        words = shlex.split(args)
    except ValueError:
        return usage
    if len(words) > 1:
        return usage
    word = words[0] if words else ""
    if word.lower() in ("", LIST):  # bare: Claude sends no arguments when Enter picks the command from the menu
        return f"{_current(provider, session)}\n\n{use_listing(provider, cwd)}"
    if word.lower() == LIVE:
        try:
            was_live = source.selected(provider, session) is None
        except UserError:
            was_live = False
        source.select_live(provider, session)
        if was_live:
            return "Current source: LIVE (unchanged)."
        return f"Current source: LIVE.\n{prefix}history-list and {prefix}history-copy use this session's own replies again."
    if _is_path(word):
        try:
            folder = _user_path(word, cwd)
        except HistoryError as exc:
            return f"Nothing selected: {exc}"
        if not folder.is_dir():
            return f"Nothing selected: {word} is not a stored session folder."
    else:
        if not NAME.fullmatch(word):
            return usage
        matches = [folder for _, folder in discovered(provider, cwd) if folder.name.lower() == word.lower()]
        if not matches:
            return (f"Nothing selected: no stored session named {word} in this folder, in {CLIENTS[provider]}'s home store "
                    f"or in this project's v2.1 store. {prefix}history-use {LIST} shows them.")
        if len(matches) > 1:
            return "\n".join([f"Nothing selected: {word} is ambiguous; stored sessions with that name are in:",
                              *[f"  {folder}" for folder in matches], f"Choose one with {prefix}history-use PATH."])
        folder = matches[0]
    label = folder.name
    try:
        turns, meta = source.select_archive(provider, session, folder)
    except UserError as exc:
        lines = [f"Nothing selected: {label} can't be used ({exc}). The current source is unchanged."]
        reopen = _reopen(_load(folder))
        if reopen:  # A still works when D can't: the original session is on this machine
            lines += ["", "Its original session is still on this machine. To reopen it yourself, in a terminal:", reopen]
        return "\n".join(lines)
    lines = [f"Current source: stored session {label} ({len(turns)} replies, {CLIENTS.get(meta.get('provider'), 'unknown client')}).",
             f"{prefix}history-list and {prefix}history-copy now use {label}. {prefix}history-use {LIVE} switches back."]
    reopen = _reopen(meta)
    if reopen:
        lines += ["", "The original session is still on this machine. To reopen it yourself, in a terminal:", reopen]
    else:
        lines += ["", "The original session isn't on this machine any more; its stored replies still work here."]
    return "\n".join(lines)


def run(provider: str, command: str, args: str, session: str, transcript: str, cwd: str) -> str:
    """Execute history-use or history-store; returns the local reply text."""
    prefix = "/" if provider == "claude" else "$"
    try:
        if command == "history-use":
            return use(provider, args, session, cwd)
        parsed = _store_args(args, prefix)
        folder, count, durable, meta = store_session(provider, session, Path(transcript), cwd, parsed.where, parsed.name, parsed.note)
        return _stored_reply(provider, cwd, folder, count, durable, meta)
    except HistoryError as exc:
        return str(exc)
    except TranscriptError as exc:  # fixed messages from the parsers, never transcript text
        return f"Stored-session error: {exc}"
    except (OSError, UnicodeError, ValueError) as exc:
        # Do not echo exception details: filenames and transcript bodies may be private.
        return f"Stored-session error: {type(exc).__name__}"
