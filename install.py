#!/usr/bin/env python3
"""Install, uninstall, or roll back agent-response-history.

  python3 install.py --provider claude|codex|both
  python3 install.py --uninstall
  python3 install.py --rollback BACKUP_DIR

Every change is recorded in BACKUP_DIR/MANIFEST.json; nothing is deleted.
"""
import argparse
import filecmp
import hashlib
import json
import os
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
# Claude slash commands this release installs; the UserPromptExpansion matcher covers exactly these.
CLAUDE_COMMANDS = ("copy-responses", "ls-responses", "store-history", "retrieve-history")
# Names the replaced projects shipped; only these are searched for, or accepted as, legacy artifacts.
LEGACY_COMMANDS = ("copy-responses", "ls-responses")
# Text that only the replaced projects wrote into their command/skill files.
LEGACY_MARKERS = (
    "response-tools/bin/response_history.py",  # claude-response-history
    "scripts/response_history.py",             # codex-response-history
    "codex-response-history:",                 # codex-response-history prompts
    "agent-response-history/run.py",           # early slash-command stubs of this project
)


# Command files shipped by earlier releases; replaced on update, removed on uninstall.
PREVIOUS_COMMANDS = {
    "c1915f4633d95fd8b8f078a22e205b9fd3559807d17d200f0133bc366b60baa0",  # 1.0.0 copy-responses.md
    "1392dd04e294a00295ed9c922ec582b64a38851af515acc069938b2e9869930f",  # 1.0.0 ls-responses.md
}


def own_command(src: Path, dst: Path) -> bool:
    return dst.is_file() and (filecmp.cmp(src, dst, shallow=False) or sha256(dst) in PREVIOUS_COMMANDS)


def release_files():
    files = [SOURCE / "run.py", SOURCE / "integrations/hook.py"]
    files += sorted(p for p in (SOURCE / "response_history").rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(p.relative_to(SOURCE) for p in files)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(path: Path) -> dict:
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    return {str(p): sha256(p) for p in files}


def is_legacy(path: Path) -> bool:
    files = [path] if path.is_file() else list(path.rglob("*.md"))
    return bool(files) and all(any(m in f.read_text(errors="replace") for m in LEGACY_MARKERS) for f in files)


class Paths:
    def __init__(self, home: Path, isolated: bool):
        env = {} if isolated else os.environ
        self.home = home
        self.claude = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        self.codex = Path(env.get("CODEX_HOME") or home / ".codex")
        self.core = home / ".local/share/agent-response-history"
        self.backups = home / ".local/share/agent-response-history-backups"
        self.hook = self.core / "integrations/hook.py"


class Transaction:
    """Applies moves/copies/JSON writes and records them for --rollback."""

    def __init__(self, paths: Paths, action: str):
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        self.dir = paths.backups / f"{stamp}-{action}"
        self.manifest = {"action": action, "created": stamp, "moved": [], "created_files": [], "json": []}

    def _ensure(self):
        self.dir.mkdir(parents=True, exist_ok=True)

    def move_aside(self, path: Path, reason: str):
        self._ensure()
        target = self.dir / "files" / str(len(self.manifest["moved"])) / path.name
        target.parent.mkdir(parents=True)
        hashes = tree_hashes(path)
        os.replace(path, target)
        self.manifest["moved"].append({"from": str(path), "to": str(target), "reason": reason, "sha256": hashes})

    def create(self, path: Path, write):
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path)
        self.manifest["created_files"].append(str(path))

    def write_json(self, path: Path, data: dict):
        self._ensure()
        before = None
        if path.exists():
            before = str(self.dir / "json" / str(len(self.manifest["json"])) / path.name)
            Path(before).parent.mkdir(parents=True)
            shutil.copy2(path, before)
        tmp = path.with_name(path.name + ".agent-response-history.tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if before:
            shutil.copymode(before, tmp)
        os.replace(tmp, path)
        self.manifest["json"].append({"file": str(path), "before": before, "after_sha256": sha256(path)})

    def commit(self) -> Path | None:
        if not (self.manifest["moved"] or self.manifest["created_files"] or self.manifest["json"]):
            return None
        self._ensure()
        (self.dir / "MANIFEST.json").write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")
        return self.dir


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError
    return data


def runs_hook(command, hook: Path) -> bool:
    """Ours is exactly `<python> -I -B <hook>`, the only form any release has written.

    Comparing parsed argv survives shell quoting; any other command, even one that ends
    with our hook path, is foreign."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False  # malformed shell string: not ours
    return len(argv) == 4 and argv[1:3] == ["-I", "-B"] and argv[3] == str(hook)


def with_hook(data: dict, event: str, group: dict | None, hook: Path) -> dict:
    """Drop every group running our hook, then add `group` (None removes)."""
    data = json.loads(json.dumps(data))
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError
    ours = lambda g: isinstance(g, dict) and any(runs_hook(h.get("command"), hook) for h in g.get("hooks", []) if isinstance(h, dict))
    groups[:] = [g for g in groups if not ours(g)] + ([group] if group else [])
    if not groups:
        del data["hooks"][event]
    if not data["hooks"]:
        del data["hooks"]
    return data


def hook_targets(paths: Paths, providers: set) -> list:
    command = " ".join(map(shlex.quote, (sys.executable, "-I", "-B", str(paths.hook))))
    handler = {"type": "command", "command": command, "timeout": 15}
    targets = []
    if "claude" in providers:
        targets.append((paths.claude / "settings.json", "UserPromptExpansion",
                        {"matcher": "|".join(CLAUDE_COMMANDS), "hooks": [handler]}))
    if "codex" in providers:
        targets.append((paths.codex / "hooks.json", "UserPromptSubmit", {"hooks": [handler]}))
    return targets


def legacy_candidates(paths: Paths, providers: set) -> list:
    found = []
    if "claude" in providers:
        found += [paths.claude / "commands/copy-response.md"]
        found += [paths.claude / "skills" / name for name in LEGACY_COMMANDS]
    if "codex" in providers:
        found += [paths.codex / "skills" / name for name in LEGACY_COMMANDS]
        found += [paths.codex / "prompts" / f"{name}.md" for name in LEGACY_COMMANDS]
        found += [paths.home / ".agents/skills" / name for name in LEGACY_COMMANDS]
    return [p for p in found if p.exists() or p.is_symlink()]


def install(paths: Paths, providers: set) -> int:
    problems, notes = [], []
    tx = Transaction(paths, "install")
    # Plan and validate everything before changing anything.
    commands = []
    if "claude" in providers:
        for name in CLAUDE_COMMANDS:
            src, dst = SOURCE / "integrations/claude" / f"{name}.md", paths.claude / "commands" / f"{name}.md"
            legacy_ok = name in LEGACY_COMMANDS and is_legacy(dst)
            if dst.is_symlink() or (dst.exists() and not own_command(src, dst) and not legacy_ok):
                problems.append(f"refusing to overwrite unrecognised {dst}")
            commands.append((src, dst))
    legacy, foreign = [], []
    for path in legacy_candidates(paths, providers):
        (legacy if not path.is_symlink() and is_legacy(path) else foreign).append(path)
    notes += [f"left untouched (not recognised as response-history): {p}" for p in foreign]
    configs = []
    for file, event, group in hook_targets(paths, providers):
        try:
            data = load_json(file)
            configs.append((file, data, with_hook(data, event, group, paths.hook)))
        except (OSError, ValueError):
            problems.append(f"cannot safely edit {file} (unreadable or unexpected JSON)")
    if paths.core.is_symlink():
        problems.append(f"refusing to replace symlink {paths.core}")
    if problems:
        print("\n".join(["Nothing changed:"] + problems), file=sys.stderr)
        return 1
    # Apply.
    for path in legacy:
        tx.move_aside(path, "legacy response-history artifact")
    wanted = release_files()
    if paths.core.exists():
        current = sorted(p.relative_to(paths.core) for p in paths.core.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        if current != wanted or not all(filecmp.cmp(SOURCE / p, paths.core / p, shallow=False) for p in wanted):
            tx.move_aside(paths.core, "previous shared helper")
    if not paths.core.exists():
        for rel in wanted:
            tx.create(paths.core / rel, lambda dst, rel=rel: shutil.copy2(SOURCE / rel, dst))
    for src, dst in commands:
        if dst.exists() and filecmp.cmp(src, dst, shallow=False):
            continue
        if dst.exists():
            tx.move_aside(dst, "legacy response-history command")
        tx.create(dst, lambda d, src=src: shutil.copy2(src, d))
    for file, before, after in configs:
        if after != before:
            tx.write_json(file, after)
    backup = tx.commit()
    for entry in tx.manifest["moved"]:
        print(f"moved {entry['from']} -> {entry['to']}")
    for line in notes:
        print(line)
    print(f"Changes recorded in {backup} (undo: python3 install.py --rollback {backup})" if backup else "Already installed; nothing changed.")
    if "claude" in providers:
        print("Claude Code: start a new session, then use /ls-responses, /copy-responses, "
              "/store-history NAME [\"NOTE\"] and /retrieve-history [NAME].")
    if "codex" in providers:
        print("Codex: 1. start or restart Codex  2. run /hooks  3. review and trust the "
              "agent-response-history UserPromptSubmit hook. Until it is trusted, $copy-responses is not intercepted. "
              "Stored sessions: $store-history NAME [\"NOTE\"], $retrieve-history list, $retrieve-history NAME.")
    return 0


def uninstall(paths: Paths) -> int:
    tx = Transaction(paths, "uninstall")
    configs = []
    for file, event, _ in hook_targets(paths, {"claude", "codex"}):
        if file.exists():
            try:
                data = load_json(file)
            except (OSError, ValueError):
                print(f"Nothing changed: cannot safely edit {file}", file=sys.stderr)
                return 1
            configs.append((file, data, with_hook(data, event, None, paths.hook)))
    for file, before, after in configs:
        if after != before:
            tx.write_json(file, after)
    for name in CLAUDE_COMMANDS:
        dst = paths.claude / "commands" / f"{name}.md"
        if not dst.is_symlink() and own_command(SOURCE / "integrations/claude" / f"{name}.md", dst):
            tx.move_aside(dst, "uninstalled command")
    if paths.core.exists() and not paths.core.is_symlink():
        tx.move_aside(paths.core, "uninstalled shared helper")
    backup = tx.commit()
    print(f"Uninstalled; recorded in {backup} (undo: python3 install.py --rollback {backup})" if backup else "Nothing to uninstall.")
    return 0


def rollback(paths: Paths, backup: Path) -> int:
    manifest = json.loads((backup / "MANIFEST.json").read_text(encoding="utf-8"))
    problems = []
    for entry in manifest["json"]:
        if Path(entry["file"]).exists() and sha256(Path(entry["file"])) != entry["after_sha256"]:
            problems.append(f"{entry['file']} changed after this run; restore it manually from {entry['before']}")
    created = set(manifest["created_files"])
    for entry in manifest["moved"]:
        path = Path(entry["from"])
        occupants = {str(p) for p in ([path] if path.is_file() else path.rglob("*")) if p.is_file()} if path.exists() else set()
        if path.is_symlink() or not occupants <= created:
            problems.append(f"{path} was changed after this run; move it away first")
    if problems:
        print("\n".join(["Nothing changed:"] + problems), file=sys.stderr)
        return 1
    tx = Transaction(paths, "rollback")
    for path in map(Path, reversed(manifest["created_files"])):
        if path.exists():
            tx.move_aside(path, f"created by {backup.name}")
    if paths.core.is_dir() and not any(p.is_file() for p in paths.core.rglob("*")):
        shutil.rmtree(paths.core)  # only empty directories remain
    for entry in reversed(manifest["moved"]):
        Path(entry["from"]).parent.mkdir(parents=True, exist_ok=True)
        os.replace(entry["to"], entry["from"])
    for entry in reversed(manifest["json"]):
        file = Path(entry["file"])
        if entry["before"]:
            shutil.copy2(entry["before"], file)
        elif file.exists():
            tx.move_aside(file, f"created by {backup.name}")
    record = tx.commit()
    print(f"Rolled back {backup}" + (f"; displaced files kept in {record}" if record else ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install agent-response-history (macOS).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--provider", choices=("claude", "codex", "both"))
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--rollback", type=Path, metavar="BACKUP_DIR")
    parser.add_argument("--home", type=Path, help="alternate home directory (for isolated testing)")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")
    paths = Paths((args.home or Path.home()).expanduser().resolve(), isolated=args.home is not None)
    if args.rollback:
        return rollback(paths, args.rollback.expanduser().resolve())
    if args.uninstall:
        return uninstall(paths)
    return install(paths, {"claude", "codex"} if args.provider == "both" else {args.provider})


if __name__ == "__main__":
    raise SystemExit(main())
