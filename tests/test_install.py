import contextlib
import hashlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import install

ROOT = Path(__file__).resolve().parents[1]
HOOK = ".local/share/agent-response-history/integrations/hook.py"
SESSIONS = ".local/share/agent-response-history-sessions"
V1_0_1 = "561b8c86dc7fb46c5a108304783319b7389f3428"
MATCHER = "history-list|history-copy|history-store|history-use"


def snapshot(home: Path) -> dict:
    """Every file under home except installer backups, with content hashes."""
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(home.rglob("*")) if p.is_file() and "agent-response-history-backups" not in p.parts}


def run(home: Path, *args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = install.main([*args, "--home", str(home)])
    return code, out.getvalue()


def ours(config: Path, event: str) -> list:
    groups = json.loads(config.read_text())["hooks"][event]
    return [g for g in groups if HOOK in json.dumps(g)]


def sessions_state(home: Path) -> dict:
    """Stored-session archive: every path with its mode, and file contents."""
    root = home / SESSIONS
    return {str(p.relative_to(root)): (stat.S_IMODE(p.lstat().st_mode), p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


def seed_sessions(home: Path) -> dict:
    """User data the installer must never touch (layout written by history_store)."""
    session = home / SESSIONS / "proj--0123456789ab" / "rocket"
    session.mkdir(parents=True)
    for folder in (home / SESSIONS, session.parent, session):
        os.chmod(folder, 0o700)
    for name, text in (("meta.json", '{"schema_version": 1, "name": "rocket"}\n'), ("replies.md", "# Session: rocket\n")):
        (session / name).write_text(text)
        os.chmod(session / name, 0o600)
    (session.parent / "empty-claim").mkdir(mode=0o700)
    state = sessions_state(home)
    assert len(state) == 6, state
    return state


def owned(config: Path, event: str, hook: Path) -> int:
    """Groups running exactly `<python> -I -B <hook>`, however the path is shell-quoted."""
    def runs(command):
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return len(argv) == 4 and argv[1:3] == ["-I", "-B"] and argv[3] == str(hook)
    groups = json.loads(config.read_text())["hooks"][event]
    return sum(any(runs(h["command"]) for h in g["hooks"]) for g in groups)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def assert_core_is_release(self):
        core = self.home / ".local/share/agent-response-history"
        installed = sorted(p.relative_to(core) for p in core.rglob("*") if p.is_file())
        self.assertEqual(installed, install.release_files())  # no .DS_Store, caches, tests or skills
        for rel in installed:
            self.assertEqual((core / rel).read_bytes(), (ROOT / rel).read_bytes())

    def test_clean_installs_reinstall_and_unrelated_settings(self):
        write(self.home / ".claude/settings.json", json.dumps({"model": "x", "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "ponytail"}]}]}}))
        write(self.home / ".codex/hooks.json", json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}]}}))
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual(code, 0, out)
        self.assert_core_is_release()
        for name in install.CLAUDE_COMMANDS:
            self.assertEqual((self.home / f".claude/commands/{name}.md").read_bytes(),
                             (ROOT / f"integrations/claude/{name}.md").read_bytes())
        self.assertEqual(sorted(p.name for p in (self.home / ".claude/commands").iterdir()),
                         ["history-copy.md", "history-list.md", "history-store.md", "history-use.md"])
        self.assertEqual([g["matcher"] for g in ours(self.home / ".claude/settings.json", "UserPromptExpansion")], [MATCHER])
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(settings["model"], "x")
        self.assertEqual(settings["hooks"]["UserPromptSubmit"], [{"hooks": [{"type": "command", "command": "ponytail"}]}])
        self.assertEqual(len(ours(self.home / ".claude/settings.json", "UserPromptExpansion")), 1)
        self.assertNotIn("/hooks", out)
        code, out = run(self.home, "--provider", "codex")
        self.assertEqual(code, 0, out)
        self.assertIn("run /hooks", out)
        hooks = json.loads((self.home / ".codex/hooks.json").read_text())
        self.assertEqual(len(hooks["hooks"]["PreToolUse"]), 1)
        self.assertEqual(len(ours(self.home / ".codex/hooks.json", "UserPromptSubmit")), 1)
        self.assertNotIn("state", hooks)  # trust is never written
        self.assertFalse((self.home / ".codex/config.toml").exists())
        for path in (".codex/skills", ".codex/prompts", ".agents"):
            self.assertFalse((self.home / path).exists(), path)
        before = snapshot(self.home)
        backups = self.home / ".local/share/agent-response-history-backups"
        count = len(list(backups.iterdir()))
        code, out = run(self.home, "--provider", "both")
        self.assertEqual((code, snapshot(self.home), len(list(backups.iterdir()))), (0, before, count))
        self.assertIn("Already installed", out)

    def test_fallback_commands_fail_closed(self):
        # Where the hook does not run, the command file is all the model sees: it must never imply success.
        hints = {"history-list": "[N | -N]", "history-copy": "[-1..-10 | N | N1,N2-N3]",
                 "history-store": "[home | PATH] [--note TEXT] [--name NAME]", "history-use": "[list | live | NAME | PATH]"}
        self.assertEqual(sorted(p.stem for p in (ROOT / "integrations/claude").glob("*.md")), sorted(install.CLAUDE_COMMANDS))
        for name, nothing in (("history-copy", "nothing was copied"), ("history-list", "nothing was listed"),
                              ("history-store", "nothing was stored"), ("history-use", "no stored session was selected")):
            head, body = (ROOT / f"integrations/claude/{name}.md").read_text().split("---", 2)[1:]
            self.assertIn(f'argument-hint: "{hints[name]}"', head)
            self.assertIn("disable-model-invocation: true", head)
            self.assertIn("hook did not run", body)
            self.assertIn(nothing, body)
            self.assertNotIn("$ARGUMENTS", body)
            for claim in ("Copied", "copied #", "Listed", "No.  Preview", "success", "Stored ", "Reopen", "Session:"):
                self.assertNotIn(claim, body)
            self.assertIn("Do not run commands, read files, or repeat earlier responses.", body)
            self.assertTrue(body.strip().splitlines()[0].startswith(f"The agent-response-history hook did not run in this client, so {nothing}"))

    def test_update_from_1_0_0_command_files(self):
        v100 = "---\ndescription: {}\nargument-hint: \"{}\"\ndisable-model-invocation: true\n---\n$ARGUMENTS\n"
        write(self.home / ".claude/commands/copy-responses.md", v100.format(
            "Copy complete responses from this Claude Code session", "[-1..-10 | n | n1,n2-n3]"))
        write(self.home / ".claude/commands/ls-responses.md", v100.format(
            "List numbered complete responses from this Claude Code session", "[count]"))
        for name in ("copy-responses", "ls-responses"):
            self.assertIn(install.sha256(self.home / f".claude/commands/{name}.md"), install.RETIRED_COMMANDS[name])
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in (self.home / ".claude/commands").iterdir()),
                         sorted(f"{name}.md" for name in install.CLAUDE_COMMANDS))
        backup = Path(out.split("--rollback ")[1].split(")")[0])
        moved = json.loads((backup / "MANIFEST.json").read_text())["moved"]
        self.assertEqual(sorted(Path(e["from"]).name for e in moved if e["reason"] == "retired command"),
                         ["copy-responses.md", "ls-responses.md"])
        write(self.home / ".claude/commands/ls-responses.md", v100.format(  # uninstall recognises 1.0.0 files
            "List numbered complete responses from this Claude Code session", "[count]"))
        write(self.home / ".claude/commands/copy-responses.md", "someone else's command")
        self.assertEqual(run(self.home, "--uninstall")[0], 0)
        self.assertFalse((self.home / ".claude/commands/ls-responses.md").exists())
        self.assertEqual((self.home / ".claude/commands/copy-responses.md").read_text(), "someone else's command")

    def test_update_replaces_only_our_helper(self):
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        stale = self.home / ".local/share/agent-response-history/response_history/cli.py"
        stale.write_text("old version\n")
        write(stale.parent / ".DS_Store", "x")
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 0, out)
        self.assert_core_is_release()
        backup = Path(out.split("--rollback ")[1].split(")")[0])
        moved = json.loads((backup / "MANIFEST.json").read_text())["moved"]
        self.assertEqual([e["reason"] for e in moved], ["previous shared helper"])
        self.assertEqual((Path(moved[0]["to"]) / "response_history/cli.py").read_text(), "old version\n")
        hooks = self.home / ".codex/hooks.json"
        data = json.loads(hooks.read_text())
        data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] = f"/old/python3 -I -B {self.home / HOOK}"
        hooks.write_text(json.dumps(data))
        self.assertEqual(run(self.home, "--provider", "codex")[0], 0)
        self.assertEqual([h["command"] for g in ours(hooks, "UserPromptSubmit") for h in g["hooks"]],
                         [f"{install.sys.executable} -I -B {self.home / HOOK}"])

    def test_legacy_migration_and_rollback(self):
        legacy = {
            ".codex/skills/copy-responses/SKILL.md": "Run scripts/response_history.py",
            ".codex/skills/copy-responses/scripts/response_history.py": "print()",
            ".codex/skills/ls-responses/SKILL.md": "sibling scripts/response_history.py",
            ".codex/prompts/copy-responses.md": "<!-- codex-response-history:copy-responses -->",
            ".codex/prompts/ls-responses.md": "<!-- codex-response-history:ls-responses -->",
            ".agents/skills/ls-responses/SKILL.md": "python3 ~/.Codex/response-tools/bin/response_history.py ls",
            ".claude/commands/copy-responses.md": "!`python3 ~/.claude/response-tools/bin/response_history.py copy`",
            ".claude/commands/copy-response.md": "!`python3 ~/.claude/response-tools/bin/response_history.py copy`",
        }
        unrelated = {
            ".agents/skills/copy-responses/SKILL.md": "someone else's skill",
            ".agents/skills/grass/SKILL.md": "unrelated",
            ".codex/prompts/other.md": "unrelated",
            ".codex/config.toml": "[hooks.state]\n",
        }
        for rel, text in {**legacy, **unrelated}.items():
            write(self.home / rel, text)
        before = snapshot(self.home)
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 0, out)
        for rel in legacy:  # all moved aside; the retired copy-responses name is not reinstalled
            self.assertFalse((self.home / rel).exists(), rel)
        for rel, text in unrelated.items():
            self.assertEqual((self.home / rel).read_text(), text)
        self.assertIn("left untouched (not recognised as response-history)", out)
        backup = Path(out.split("--rollback ")[1].split(")")[0])
        manifest = json.loads((backup / "MANIFEST.json").read_text())
        moved = {Path(e["from"]).relative_to(self.home).as_posix() for e in manifest["moved"]}
        self.assertEqual(moved, {".codex/skills/copy-responses", ".codex/skills/ls-responses", ".codex/prompts/copy-responses.md",
                                 ".codex/prompts/ls-responses.md", ".agents/skills/ls-responses",
                                 ".claude/commands/copy-response.md", ".claude/commands/copy-responses.md"})
        self.assertTrue(all(e["sha256"] for e in manifest["moved"]))
        code, out = run(self.home, "--rollback", str(backup))
        self.assertEqual(code, 0, out)
        self.assertEqual(snapshot(self.home), before)

    def test_refuses_unknown_command_and_bad_json(self):
        write(self.home / ".claude/commands/ls-responses.md", "my own command")  # retired name, unrecognised content
        before = snapshot(self.home)
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual((code, snapshot(self.home)), (1, before))
        self.assertIn("refusing to retire unrecognised", out)
        self.assertFalse((self.home / ".local/share").exists())
        (self.home / ".claude/commands/ls-responses.md").unlink()
        write(self.home / ".claude/commands/history-list.md", "my own command")  # current name, someone else's file
        before = snapshot(self.home)
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual((code, snapshot(self.home)), (1, before))
        self.assertIn("refusing to overwrite unrecognised", out)
        self.assertFalse((self.home / ".local/share").exists())
        write(self.home / ".codex/hooks.json", "{not json")
        code, out = run(self.home, "--provider", "codex")
        self.assertEqual(code, 1)
        self.assertIn("cannot safely edit", out)

    def test_uninstall_and_rollback_of_uninstall(self):
        write(self.home / ".codex/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}))
        before = snapshot(self.home)
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        installed = snapshot(self.home)
        code, out = run(self.home, "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / ".local/share/agent-response-history").exists())
        self.assertFalse((self.home / ".claude/commands/history-copy.md").exists())
        self.assertEqual(json.loads((self.home / ".codex/hooks.json").read_text()),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}})
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text()), {})
        backup = Path(out.split("--rollback ")[1].split(")")[0])
        self.assertEqual(run(self.home, "--rollback", str(backup))[0], 0)
        self.assertEqual(snapshot(self.home), installed)
        self.assertNotEqual(before, installed)

    def test_release_file_set(self):
        rels = {p.as_posix() for p in install.release_files()}
        self.assertEqual(rels, {"run.py", "integrations/hook.py"} | {
            f"response_history/{name}.py" for name in ("__init__", "cli", "clipboard", "archive", "history_store", "model", "selection", "session", "source", "transcript")} | {
            f"response_history/adapters/{name}.py" for name in ("__init__", "claude", "codex", "common")})
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        self.assert_core_is_release()  # byte-identical to the branch source; no .md, tests, caches or .git

    def test_stored_sessions_survive_install_reinstall_uninstall_and_rollbacks(self):
        sessions = seed_sessions(self.home)
        code, out = run(self.home, "--provider", "both")
        self.assertEqual(code, 0, out)
        self.assertEqual(sessions_state(self.home), sessions)
        install_backup = Path(out.split("--rollback ")[1].split(")")[0])
        stale = self.home / ".local/share/agent-response-history/response_history/cli.py"
        stale.write_text("old\n")  # force a real reinstall that replaces the helper tree
        self.assertEqual(run(self.home, "--provider", "both")[0], 0)
        self.assertEqual(sessions_state(self.home), sessions)
        installed = snapshot(self.home)
        code, out = run(self.home, "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(sessions_state(self.home), sessions)
        self.assertFalse((self.home / ".local/share/agent-response-history").exists())
        uninstall_backup = Path(out.split("--rollback ")[1].split(")")[0])
        backups = self.home / ".local/share/agent-response-history-backups"
        self.assertFalse(any("rocket" in p.name or "agent-response-history-sessions" in p.name for p in backups.rglob("*")))
        self.assertEqual(run(self.home, "--rollback", str(uninstall_backup))[0], 0)
        self.assertEqual((snapshot(self.home), sessions_state(self.home)), (installed, sessions))
        self.assertEqual(run(self.home, "--uninstall")[0], 0)
        self.assertEqual(run(self.home, "--provider", "claude")[0], 0)
        code, out = run(self.home, "--provider", "both")
        backup = Path(out.split("--rollback ")[1].split(")")[0])
        self.assertEqual(run(self.home, "--rollback", str(backup))[0], 0)  # rollback of an install
        self.assertEqual(sessions_state(self.home), sessions)
        self.assertTrue(install_backup.is_dir())

    def test_new_command_names_are_never_taken_over(self):
        for name in install.CLAUDE_COMMANDS:
            with self.subTest(name=name):
                home = Path(self.tmp.name).resolve() / f"foreign-{name}"
                dst = home / f".claude/commands/{name}.md"
                # Even text carrying a legacy marker is foreign under a name the old projects never shipped.
                write(dst, "run python3 ~/.local/share/agent-response-history/run.py for my own tool\n")
                before = snapshot(home)
                code, out = run(home, "--provider", "claude")
                self.assertEqual((code, snapshot(home)), (1, before), out)
                self.assertIn(f"refusing to overwrite unrecognised {dst}", out)
                self.assertFalse((home / ".local/share").exists())
                dst.unlink()
                target = home / "elsewhere.md"
                write(target, (ROOT / f"integrations/claude/{name}.md").read_text())  # identical bytes, still a symlink
                dst.symlink_to(target)
                before = snapshot(home)
                code, out = run(home, "--provider", "both")
                self.assertEqual((code, snapshot(home), dst.is_symlink()), (1, before, True), out)
                self.assertFalse((home / ".local/share").exists())

    def test_uninstall_leaves_modified_new_commands(self):
        self.assertEqual(run(self.home, "--provider", "claude")[0], 0)
        mine = self.home / ".claude/commands/history-store.md"
        mine.write_text("my edited command\n")
        code, out = run(self.home, "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(mine.read_text(), "my edited command\n")
        self.assertEqual(sorted(p.name for p in (self.home / ".claude/commands").iterdir()), ["history-store.md"])
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text()), {})

    def test_upgrade_from_real_v1_0_1_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            v1 = Path(tmp) / "v1"
            v1.mkdir()
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", V1_0_1], capture_output=True)
            if archive.returncode:
                self.skipTest("v1.0.1 commit unavailable (not a git checkout of agent-response-history)")
            subprocess.run(["tar", "-x", "-C", str(v1)], input=archive.stdout, check=True)
            write(self.home / ".claude/settings.json", json.dumps({"model": "x", "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "keep-claude"}]}]}}))
            write(self.home / ".codex/hooks.json", json.dumps({"hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-codex"}]}]}}))
            write(self.home / ".codex/config.toml", "model = \"x\"\n")
            old = subprocess.run([sys.executable, str(v1 / "install.py"), "--provider", "both", "--home", str(self.home)],
                                 capture_output=True, text=True)
            self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
            core = self.home / ".local/share/agent-response-history"
            self.assertFalse((core / "response_history/history_store.py").exists())
            sessions = seed_sessions(self.home)
            v1_state = snapshot(self.home)
            codex_hooks = (self.home / ".codex/hooks.json").read_bytes()
            code, out = run(self.home, "--provider", "both")
            self.assertEqual(code, 0, out)
            self.assert_core_is_release()
            backup = Path(out.split("--rollback ")[1].split(")")[0])
            manifest = json.loads((backup / "MANIFEST.json").read_text())
            self.assertEqual(sorted(e["reason"] for e in manifest["moved"]), ["previous shared helper", "retired command", "retired command"])
            old_core = Path(next(e for e in manifest["moved"] if e["reason"] == "previous shared helper")["to"])
            self.assertEqual(sorted(p.relative_to(old_core).as_posix() for p in old_core.rglob("*") if p.is_file()),
                             sorted(p.relative_to(v1).as_posix() for p in v1.rglob("*.py")
                                    if p.parts[len(v1.parts)] in ("response_history", "run.py") or p.relative_to(v1).as_posix() == "integrations/hook.py"))
            for p in old_core.rglob("*.py"):
                self.assertEqual(p.read_bytes(), (v1 / p.relative_to(old_core)).read_bytes())
            for name in install.CLAUDE_COMMANDS:
                self.assertEqual((self.home / f".claude/commands/{name}.md").read_bytes(),
                                 (ROOT / f"integrations/claude/{name}.md").read_bytes())
            settings = json.loads((self.home / ".claude/settings.json").read_text())
            self.assertEqual([g["matcher"] for g in ours(self.home / ".claude/settings.json", "UserPromptExpansion")], [MATCHER])
            self.assertEqual((settings["model"], settings["hooks"]["Stop"]), ("x", [{"hooks": [{"type": "command", "command": "keep-claude"}]}]))
            self.assertEqual(len(ours(self.home / ".codex/hooks.json", "UserPromptSubmit")), 1)
            self.assertEqual((self.home / ".codex/hooks.json").read_bytes(), codex_hooks)  # identical hook definition; nothing rewritten
            self.assertEqual((self.home / ".codex/config.toml").read_text(), "model = \"x\"\n")
            for path in (".codex/skills", ".codex/prompts", ".agents", ".claude/skills"):
                self.assertFalse((self.home / path).exists(), path)
            self.assertEqual(sessions_state(self.home), sessions)
            code, out = run(self.home, "--rollback", str(backup))
            self.assertEqual(code, 0, out)
            self.assertEqual(snapshot(self.home), v1_state)
            self.assertEqual(sessions_state(self.home), sessions)

    def test_hook_ownership_survives_quotes_in_paths(self):
        # Ours is exactly `<python> -I -B <hook>` after shell parsing: quoting of a home containing '
        # cannot hide our hook, and a foreign command that mentions or ends with it is not ours.
        for name in ("it's home", 'home with "double" quotes and space', "plain space"):
            with self.subTest(home=name):
                home = Path(self.tmp.name).resolve() / name
                hook = home / HOOK
                others = [{"hooks": [{"type": "command", "command": "ponytail"}]},
                          {"hooks": [{"type": "command", "command": f"echo {hook} >> log"}]},  # mentions our hook, is not ours
                          {"hooks": [{"type": "command", "command": "python3 'unterminated"}]},  # malformed shell string
                          {"hooks": [{"type": "command", "command": f"wrap {shlex.quote(str(hook))} --debug"}]},  # ours, not last
                          {"hooks": [{"type": "command", "command": f"python3 {shlex.quote('/backup' + str(hook))}"}]},  # suffix only
                          {"hooks": [{"type": "command", "command": f"echo {shlex.quote(str(hook))}"}]},  # ends with it, not ours
                          {"hooks": [{"type": "command", "command": f"python3 -I -B {shlex.quote(str(hook))} --x"}]}]  # extra arg
                foreign = {"UserPromptSubmit": others, "UserPromptExpansion": others, "Stop": others}
                write(home / ".claude/settings.json", json.dumps({"model": "x", "hooks": foreign}))
                write(home / ".codex/hooks.json", json.dumps({"hooks": foreign}))
                code, out = run(home, "--provider", "both")
                self.assertEqual(code, 0, out)
                self.assertEqual(owned(home / ".claude/settings.json", "UserPromptExpansion", hook), 1)
                self.assertEqual(owned(home / ".codex/hooks.json", "UserPromptSubmit", hook), 1)
                installed = {f: (home / f).read_bytes() for f in (".claude/settings.json", ".codex/hooks.json")}
                code, out = run(home, "--provider", "both")
                self.assertEqual(code, 0, out)
                self.assertIn("Already installed", out)
                self.assertEqual({f: (home / f).read_bytes() for f in installed}, installed)
                self.assertEqual(run(home, "--uninstall")[0], 0)
                self.assertEqual(json.loads((home / ".claude/settings.json").read_text()), {"model": "x", "hooks": foreign})
                self.assertEqual(json.loads((home / ".codex/hooks.json").read_text()), {"hooks": foreign})

    def test_hook_ownership_with_quoted_config_dirs(self):
        # Non-isolated mode: HOME, CLAUDE_CONFIG_DIR and CODEX_HOME all contain a single quote.
        home = Path(self.tmp.name).resolve() / "user's home"
        env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "claude's config"),
               "CODEX_HOME": str(home / "codex's home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for attempt in range(2):
            r = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"), "--provider", "both"],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Already installed", r.stdout)
        self.assertEqual(owned(home / "claude's config/settings.json", "UserPromptExpansion", home / HOOK), 1)
        self.assertEqual(owned(home / "codex's home/hooks.json", "UserPromptSubmit", home / HOOK), 1)


if __name__ == "__main__":
    unittest.main()
