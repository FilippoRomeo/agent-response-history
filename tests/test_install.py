import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import install

ROOT = Path(__file__).resolve().parents[1]
HOOK = ".local/share/agent-response-history/integrations/hook.py"


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
        for name in ("copy-responses", "ls-responses"):
            self.assertEqual((self.home / f".claude/commands/{name}.md").read_bytes(),
                             (ROOT / f"integrations/claude/{name}.md").read_bytes())
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
        for name, nothing in (("copy-responses", "nothing was copied"), ("ls-responses", "nothing was listed")):
            body = (ROOT / f"integrations/claude/{name}.md").read_text().split("---", 2)[2]
            self.assertIn("hook did not run", body)
            self.assertIn(nothing, body)
            self.assertNotIn("$ARGUMENTS", body)
            for claim in ("Copied", "copied #", "Listed", "No.  Preview", "success"):
                self.assertNotIn(claim, body)

    def test_update_from_1_0_0_command_files(self):
        v100 = "---\ndescription: {}\nargument-hint: \"{}\"\ndisable-model-invocation: true\n---\n$ARGUMENTS\n"
        write(self.home / ".claude/commands/copy-responses.md", v100.format(
            "Copy complete responses from this Claude Code session", "[-1..-10 | n | n1,n2-n3]"))
        write(self.home / ".claude/commands/ls-responses.md", v100.format(
            "List numbered complete responses from this Claude Code session", "[count]"))
        self.assertEqual({install.sha256(p) for p in (self.home / ".claude/commands").iterdir()}, install.PREVIOUS_COMMANDS)
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual(code, 0, out)
        for name in ("copy-responses", "ls-responses"):
            self.assertEqual((self.home / f".claude/commands/{name}.md").read_bytes(),
                             (ROOT / f"integrations/claude/{name}.md").read_bytes())
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
        for rel in legacy:
            if rel != ".claude/commands/copy-responses.md":
                self.assertFalse((self.home / rel).exists(), rel)
        self.assertEqual((self.home / ".claude/commands/copy-responses.md").read_bytes(),
                         (ROOT / "integrations/claude/copy-responses.md").read_bytes())
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
        write(self.home / ".claude/commands/ls-responses.md", "my own command")
        before = snapshot(self.home)
        code, out = run(self.home, "--provider", "claude")
        self.assertEqual((code, snapshot(self.home)), (1, before))
        self.assertIn("refusing to overwrite", out)
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
        self.assertFalse((self.home / ".claude/commands/copy-responses.md").exists())
        self.assertEqual(json.loads((self.home / ".codex/hooks.json").read_text()),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}})
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text()), {})
        backup = Path(out.split("--rollback ")[1].split(")")[0])
        self.assertEqual(run(self.home, "--rollback", str(backup))[0], 0)
        self.assertEqual(snapshot(self.home), installed)
        self.assertNotEqual(before, installed)


if __name__ == "__main__":
    unittest.main()
