"""v2.1 -> history-* migration: command retirement, state lifecycle, archives left alone."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import install
from response_history.adapters import claude as claude_adapter, codex as codex_adapter
from tests.test_install import HOOK, MATCHER, ROOT, ours, run, snapshot, write

CURRENT = ("history-list", "history-copy", "history-store", "history-use")
RETIRED = ("copy-responses", "ls-responses", "store-history", "retrieve-history")
STATE = ".local/state/agent-response-history"
SESSION = "00000000-0000-4000-8000-00000000abcd"
# Pinned independently of install.py: full SHA-256 of every command file ever shipped under each name.
SHIPPED = {
    "copy-responses": {"c1915f4633d95fd8b8f078a22e205b9fd3559807d17d200f0133bc366b60baa0",
                       "1a0241245e224eaa4bf849052070599d6b7af4280f75bffd9fdf8dfd5ca3cd81"},
    "ls-responses": {"1392dd04e294a00295ed9c922ec582b64a38851af515acc069938b2e9869930f",
                     "e3bf87f801dc55654513decd60589c9b4235c4b92f24314a118ae20cbc2ff149"},
    "store-history": {"8945af74f2652abd51bdfb7ee45f97ec4b563da5b301c8d2dd1c6e3fee45815c"},
    "retrieve-history": {"c0aa5abe9feafd3d34fec07f59635efb7997d20f593480b0133721eb16690ca9"},
}
RELEASES = {"9f532d95ea007d0053891a806196780c9de0cfd4": ("copy-responses", "ls-responses"),  # 1.0.0
            "v1.0.1": ("copy-responses", "ls-responses"),
            "v2.0.0": RETIRED, "v2.1.0": RETIRED}


def git_show(ref: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_mode & 0o777, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class HashTests(unittest.TestCase):
    def test_retired_hashes_are_pinned_in_full(self):
        self.assertEqual(install.RETIRED_COMMANDS, SHIPPED)
        self.assertEqual(install.CLAUDE_COMMANDS, CURRENT)
        self.assertEqual(install.PREVIOUS_VERSIONS, {})
        self.assertTrue(all(len(h) == 64 for hashes in SHIPPED.values() for h in hashes))

    def test_hashes_match_the_tagged_releases(self):
        seen = {name: set() for name in RETIRED}
        for ref, names in RELEASES.items():
            for name in names:
                data = git_show(ref, f"integrations/claude/{name}.md")
                if data is None:
                    self.skipTest(f"{ref} unavailable (not a git checkout with tags)")
                seen[name].add(hashlib.sha256(data).hexdigest())
        self.assertEqual(seen, SHIPPED)
        for name in RETIRED:  # retired files are gone from the source tree
            self.assertFalse((ROOT / f"integrations/claude/{name}.md").exists())

    def test_recognition_is_per_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            ls_bytes = git_show("v2.1.0", "integrations/claude/ls-responses.md")
            if ls_bytes is None:
                self.skipTest("v2.1.0 unavailable")
            for wrong in ("store-history.md", "history-list.md"):  # the ls-responses hash authorises only ls-responses.md
                with self.subTest(path=wrong):
                    dst = home / ".claude/commands" / wrong
                    write(dst, "")
                    dst.write_bytes(ls_bytes)
                    before = snapshot(home)
                    code, out = run(home, "--provider", "claude")
                    self.assertEqual((code, snapshot(home)), (1, before), out)
                    dst.unlink()
            marked = "!`python3 ~/.claude/response-tools/bin/response_history.py copy`"  # a replaced project's command
            for name, accepted in (("copy-responses", True), ("store-history", False)):  # markers count only where it shipped
                with self.subTest(marked=name):
                    dst = home / ".claude/commands" / f"{name}.md"
                    write(dst, marked)
                    self.assertEqual(install.retired_command(name, dst), accepted)
                    dst.unlink()


class TranscriptFilterTests(unittest.TestCase):
    def test_historical_and_current_names_are_both_filtered(self):
        for name in RETIRED + CURRENT:
            with self.subTest(name=name):
                rows = [{"type": "user", "uuid": "u", "message": {"content": f"<command-name>/{name}</command-name>"}}]
                self.assertEqual(claude_adapter.parse(rows, "s")[0].excluded, "helper command")
                rows = [{"type": "response_item", "payload": {"type": "message", "role": "user",
                                                              "content": [{"type": "input_text", "text": f"${name} 2"}]}}]
                self.assertEqual(codex_adapter.parse(rows, "s")[0].excluded, "helper command")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home, self.external = base / "home", base / "external-store"
        self.home.mkdir()
        self.backups = self.home / ".local/share/agent-response-history-backups"

    def tearDown(self):
        self.tmp.cleanup()

    # ---- fixtures -----------------------------------------------------------------------

    def install_v21(self):
        src = Path(self.tmp.name) / "v2.1.0"
        src.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", "v2.1.0"], capture_output=True)
        if archive.returncode:
            self.skipTest("v2.1.0 unavailable (not a git checkout with tags)")
        subprocess.run(["tar", "-x", "-C", str(src)], input=archive.stdout, check=True)
        write(self.home / ".claude/settings.json", json.dumps({"model": "x", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "keep-claude"}]}]}}))
        write(self.home / ".codex/hooks.json", json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-codex"}]}]}}))
        write(self.home / ".claude/commands/mine.md", "an unrelated command\n")
        r = subprocess.run([sys.executable, str(src / "install.py"), "--provider", "both", "--home", str(self.home)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name in RETIRED:
            self.assertEqual((self.home / f".claude/commands/{name}.md").read_bytes(),
                             git_show("v2.1.0", f"integrations/claude/{name}.md"))
        return src

    def seed_archives(self) -> dict:
        """User archives in every kind of store; the installer must never touch any of them."""
        roots = {
            "legacy": self.home / ".local/share/agent-response-history-sessions/proj--0123456789ab",
            "claude-home": self.home / ".claude/agent-response-history",
            "codex-home": self.home / ".codex/agent-response-history",
            "project": self.home / "work/proj/.agent-response-history",
            "external": self.external,
        }
        for label, root in roots.items():
            folder = root / f"{label}-archive"
            folder.mkdir(parents=True, mode=0o700)
            meta = {"schema_version": 2, "name": folder.name, "provider": "claude", "session_id": SESSION,
                    "cwd": str(self.home), "reply_count": 1}
            for name, text in (("meta.json", json.dumps(meta)), ("turns.jsonl", json.dumps({"prompt": "p", "response": f"r-{label}"}) + "\n"),
                               ("replies.md", f"# Session: {folder.name}\n"), (".gitignore", "*\n")):
                (folder / name).write_text(text)
                os.chmod(folder / name, 0o600)
        self.archive_roots = roots
        return self.archives()

    def archives(self) -> dict:
        return {label: tree(root) for label, root in self.archive_roots.items()}

    def installed_hook(self, name, args):
        """Run the hook the installer put in place, as the client would."""
        event = {"hook_event_name": "UserPromptExpansion", "session_id": SESSION, "cwd": str(self.home),
                 "transcript_path": str(self.home / f"{SESSION}.jsonl"), "command_name": name, "command_args": args}
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        r = subprocess.run([sys.executable, "-I", "-B", str(self.home / HOOK)], input=json.dumps(event),
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["reason"] if r.stdout.strip() else None

    def make_state(self) -> dict:
        reply = self.installed_hook("history-use", f"'{self.archive_roots['external'] / 'external-archive'}'")
        self.assertTrue(reply.startswith("Current source: stored session external-archive"), reply)
        state = self.home / STATE
        self.assertTrue(any(p.is_file() for p in state.rglob("*.json")))
        return tree(state)

    def backup_of(self, out: str) -> Path:
        return Path(out.split("--rollback ")[1].split(")")[0])

    def moved(self, backup: Path) -> dict:
        return {Path(e["from"]).relative_to(self.home).as_posix(): e["reason"]
                for e in json.loads((backup / "MANIFEST.json").read_text())["moved"]}

    def assert_new_install(self):
        commands = self.home / ".claude/commands"
        self.assertEqual(sorted(p.name for p in commands.iterdir()), sorted([f"{n}.md" for n in CURRENT] + ["mine.md"]))
        for name in CURRENT:
            self.assertEqual((commands / f"{name}.md").read_bytes(), (ROOT / f"integrations/claude/{name}.md").read_bytes())
        self.assertEqual([g["matcher"] for g in ours(self.home / ".claude/settings.json", "UserPromptExpansion")], [MATCHER])
        core = self.home / ".local/share/agent-response-history"
        self.assertEqual(sorted(p.relative_to(core) for p in core.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
                         install.release_files())
        for rel in install.release_files():
            self.assertEqual((core / rel).read_bytes(), (ROOT / rel).read_bytes())
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual((settings["model"], settings["hooks"]["Stop"]), ("x", [{"hooks": [{"type": "command", "command": "keep-claude"}]}]))
        codex = json.loads((self.home / ".codex/hooks.json").read_text())
        self.assertEqual(codex["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-codex"}]}])
        self.assertEqual(len(ours(self.home / ".codex/hooks.json", "UserPromptSubmit")), 1)

    # ---- tests ----------------------------------------------------------------------------

    def test_fresh_install(self):
        write(self.home / ".claude/settings.json", json.dumps({"model": "x", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "keep-claude"}]}]}}))
        write(self.home / ".codex/hooks.json", json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-codex"}]}]}}))
        write(self.home / ".claude/commands/mine.md", "an unrelated command\n")
        archives = self.seed_archives()
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 0, out)
        self.assert_new_install()
        for old in RETIRED:
            self.assertNotIn(old, out)
            self.assertFalse((self.home / f".claude/commands/{old}.md").exists())
        for shim in (".codex/skills", ".codex/prompts", ".agents", ".claude/skills"):
            self.assertFalse((self.home / shim).exists(), shim)
        for example in ("/history-list", "/history-copy", "/history-store", "/history-use list",
                        "$history-list 10", "$history-copy -1", "$history-use list", "$history-store home"):
            self.assertIn(example, out)
        self.assertFalse((self.home / STATE).exists())
        self.assertEqual(self.archives(), archives)
        self.assertIsNone(self.installed_hook("copy-responses", "-1"))

    def test_v21_upgrade_rollback_to_v21(self):
        self.install_v21()
        archives = self.seed_archives()
        write(self.home / STATE / "sources/claude/keep.json", '{"kind": "archive", "path": "/x", "name": "x"}')  # existing state
        state_before = tree(self.home / STATE)
        v21 = snapshot(self.home)
        v21_core = {p: (self.home / p).read_bytes() for p in v21 if p.startswith(".local/share/agent-response-history/")}
        v21_configs = {f: (self.home / f).read_bytes() for f in (".claude/settings.json", ".codex/hooks.json")}
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 0, out)
        backup = self.backup_of(out)
        self.assert_new_install()
        moved = self.moved(backup)
        self.assertEqual({k: v for k, v in moved.items() if v == "retired command"},
                         {f".claude/commands/{n}.md": "retired command" for n in RETIRED})
        self.assertEqual(tree(self.home / STATE), state_before)  # update preserves state untouched
        self.assertEqual(self.archives(), archives)
        for old in RETIRED:  # the installed hook no longer answers the old names
            self.assertIsNone(self.installed_hook(old, ""))
        self.assertIsNotNone(self.installed_hook("history-list", ""))
        self.make_state()
        state_after_use = tree(self.home / STATE)
        code, out = run(self.home, "--rollback", str(backup))
        self.assertEqual(code, 0, out)
        # commands, core, configs, unrelated files: all as before; only the state is gone from its active place
        self.assertEqual(snapshot(self.home), {k: v for k, v in v21.items() if not k.startswith(STATE)})
        for rel, data in v21_core.items():
            self.assertEqual((self.home / rel).read_bytes(), data)
        for rel, data in v21_configs.items():
            self.assertEqual((self.home / rel).read_bytes(), data)
        for name in RETIRED:
            self.assertEqual((self.home / f".claude/commands/{name}.md").read_bytes(), git_show("v2.1.0", f"integrations/claude/{name}.md"))
        for name in CURRENT:
            self.assertFalse((self.home / f".claude/commands/{name}.md").exists())
        self.assertFalse((self.home / STATE).exists())  # no longer active...
        record = Path(out.split("displaced files kept in ")[1].strip())
        kept = [Path(e["to"]) for e in json.loads((record / "MANIFEST.json").read_text())["moved"] if e["from"] == str(self.home / STATE)]
        self.assertEqual(len(kept), 1)
        self.assertEqual(tree(kept[0]), state_after_use)  # ...but kept, byte for byte, in the rollback backup
        self.assertEqual(self.archives(), archives)

    def test_unrecognised_retired_file_refuses_before_any_change(self):
        self.install_v21()
        archives = self.seed_archives()
        mine = self.home / ".claude/commands/store-history.md"
        mine.write_text(mine.read_text() + "\nmy edit\n")
        before, backups = snapshot(self.home), sorted(self.backups.iterdir())
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("Nothing changed:"), out)
        self.assertIn(f"refusing to retire unrecognised {mine}", out)
        self.assertEqual((snapshot(self.home), sorted(self.backups.iterdir())), (before, backups))
        for name in CURRENT:
            self.assertFalse((self.home / f".claude/commands/{name}.md").exists())
        self.assertEqual(self.archives(), archives)

    def test_retired_symlink_refuses(self):
        self.install_v21()
        link = self.home / ".claude/commands/retrieve-history.md"
        target = self.home / "elsewhere.md"
        target.write_bytes(link.read_bytes())  # identical recognised bytes, but a symlink
        link.unlink()
        link.symlink_to(target)
        before = snapshot(self.home)
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual((code, snapshot(self.home), link.is_symlink()), (1, before, True), out)

    def test_uninstall_moves_state_keeps_archives_and_rollback_restores(self):
        write(self.home / ".claude/settings.json", json.dumps({"model": "x", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "keep-claude"}]}]}}))
        write(self.home / ".codex/hooks.json", json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-codex"}]}]}}))
        write(self.home / ".claude/commands/mine.md", "an unrelated command\n")
        archives = self.seed_archives()
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        state = self.make_state()
        leftover = self.home / ".claude/commands/copy-responses.md"  # a recognised retired file somehow left behind
        leftover.write_bytes(git_show("9f532d95ea007d0053891a806196780c9de0cfd4", "integrations/claude/copy-responses.md") or b"")
        foreign = self.home / ".claude/commands/ls-responses.md"
        foreign.write_text("someone else's ls-responses\n")
        installed = snapshot(self.home)
        code, out = run(self.home, "--uninstall")
        self.assertEqual(code, 0, out)
        backup = self.backup_of(out)
        self.assertEqual(sorted(p.name for p in (self.home / ".claude/commands").iterdir()), ["ls-responses.md", "mine.md"])
        self.assertEqual(foreign.read_text(), "someone else's ls-responses\n")
        self.assertFalse((self.home / ".local/share/agent-response-history").exists())
        self.assertFalse((self.home / STATE).exists())
        moved = self.moved(backup)
        self.assertEqual(moved[STATE], "response-source state")
        self.assertEqual(moved[".claude/commands/copy-responses.md"], "retired command")
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text()),
                         {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-claude"}]}]}})
        self.assertEqual(self.archives(), archives)
        code, out = run(self.home, "--rollback", str(backup))
        self.assertEqual(code, 0, out)
        self.assertEqual(snapshot(self.home), installed)
        self.assertEqual(tree(self.home / STATE), state)
        self.assertEqual(self.archives(), archives)

    def test_unsafe_state_path_refuses_uninstall_and_rollback(self):
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        code, out = run(self.home, "--provider", "claude")  # an install to roll back
        elsewhere = self.home / "elsewhere-state"
        elsewhere.mkdir()
        (self.home / STATE).parent.mkdir(parents=True)
        (self.home / STATE).symlink_to(elsewhere)
        before = snapshot(self.home)
        code, out = run(self.home, "--uninstall")
        self.assertEqual((code, snapshot(self.home)), (1, before), out)
        self.assertIn("not a plain folder", out)
        backups = sorted(self.backups.iterdir())
        code, out = run(self.home, "--rollback", str(backups[0]))
        self.assertEqual((code, snapshot(self.home)), (1, before), out)
        self.assertTrue((self.home / STATE).is_symlink())

    def test_update_never_touches_state_or_archives(self):
        archives = self.seed_archives()
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        state = self.make_state()
        (self.home / ".local/share/agent-response-history/run.py").write_text("stale\n")  # force a real update
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 0, out)
        self.assertIn("previous shared helper", json.dumps(self.moved(self.backup_of(out))))
        self.assertEqual(tree(self.home / STATE), state)
        self.assertEqual(self.archives(), archives)


if __name__ == "__main__":
    unittest.main()
