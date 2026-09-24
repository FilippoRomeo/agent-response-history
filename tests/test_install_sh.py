import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL = [Path.home() / p for p in (".claude/settings.json", ".codex/config.toml", ".codex/hooks.json", ".codex/auth.json")]


def stub(path: Path, body: str):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def old_python(path: Path):
    """A working interpreter that reports version 3.9 to -c code, like a real old python3."""
    py = shlex.quote(sys.executable)
    stub(path, f'if [ "$1" = -c ]; then exec {py} -c "import sys; sys.version_info = (3, 9, 0, \'final\', 0); exec(sys.argv[1])" "$2"; fi\n'
               f'exec {py} "$@"')


def hook_python(config: Path, event: str) -> list:
    """Interpreters of our `<python> -I -B <hook>` groups; the hook belongs to the config's own home."""
    hook = str(config.parent.parent / ".local/share/agent-response-history/integrations/hook.py")
    found = []
    for group in json.loads(config.read_text())["hooks"][event]:
        for h in group["hooks"]:
            try:
                argv = shlex.split(h["command"])
            except (ValueError, TypeError, AttributeError):
                continue
            if len(argv) == 4 and argv[1:3] == ["-I", "-B"] and argv[3] == hook:
                found.append(argv[0])
    return found


class InstallShTests(unittest.TestCase):
    """install.sh picks the interpreter and the providers, then hands everything to install.py."""

    @classmethod
    def setUpClass(cls):
        cls.real = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in REAL if p.is_file()}

    @classmethod
    def tearDownClass(cls):
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in REAL if p.is_file()} == cls.real, "real config changed"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bins = {}
        for name, clients in (("both", ("claude", "codex")), ("claude", ("claude",)), ("codex", ("codex",)), ("none", ())):
            b = self.base / f"bin-{name}"
            b.mkdir()
            for client in clients:
                stub(b / client, "exit 0")
            old_python(b / "python3")  # a working but too-old python3 comes first on PATH
            (b / "python3.12").symlink_to(sys.executable)
            self.bins[name] = b
        self.good = str(self.bins["both"] / "python3.12")

    def run_sh(self, *args, bin="both", repo=ROOT, cwd=None, extra_path=(), env=None):
        env = {"PATH": os.pathsep.join([*map(str, extra_path), str(self.bins[bin]), "/usr/bin", "/bin"]),
               "HOME": str(self.base / "not-home"), **(env or {})}  # a bug can never reach the real home
        return subprocess.run([str(repo / "install.sh"), *args], capture_output=True, text=True, env=env, cwd=cwd or self.base)

    def home(self, name):
        return self.base / name

    def register(self, home, provider, name):
        """Install with a named interpreter, as an earlier release would have registered it."""
        python = self.base / name
        if not python.exists():
            python.symlink_to(sys.executable)
        subprocess.run([str(python), str(ROOT / "install.py"), "--provider", provider, "--home", str(home)], check=True, capture_output=True)
        return str(python)

    def test_detects_both_and_skips_too_old_python3(self):
        h = self.home("a")
        r = self.run_sh("--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [self.good])
        self.assertEqual(hook_python(h / ".claude/settings.json", "UserPromptExpansion"), [self.good])

    def test_reuses_registered_interpreter_so_the_hook_is_unchanged(self):
        h = self.home("b")
        other = self.base / "registered-python"
        other.symlink_to(sys.executable)
        subprocess.run([str(other), str(ROOT / "install.py"), "--provider", "both", "--home", str(h)], check=True, capture_output=True)
        before = (h / ".codex/hooks.json").read_bytes()
        for flag in (["--home", str(h)], [f"--home={h}"]):
            r = self.run_sh(*flag)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Already installed; nothing changed.", r.stdout)
            self.assertEqual((h / ".codex/hooks.json").read_bytes(), before)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [str(other)])

    def test_missing_registered_interpreter_falls_back(self):
        h = self.home("c")
        self.assertEqual(self.run_sh("--home", str(h)).returncode, 0)
        for config in (h / ".codex/hooks.json", h / ".claude/settings.json"):
            config.write_text(config.read_text().replace(self.good, "/nonexistent/python3"))
        r = self.run_sh("--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [self.good])

    def test_only_claude_on_path(self):
        h = self.home("d")
        r = self.run_sh("--home", str(h), bin="claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((h / ".claude/settings.json").is_file())
        self.assertFalse((h / ".codex").exists())

    def test_no_client_on_path_changes_nothing(self):
        h = self.home("e")
        r = self.run_sh("--home", str(h), bin="none")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Neither claude nor codex is on PATH", r.stderr)
        self.assertFalse(h.exists())

    def test_uninstall_rollback_and_help_pass_through(self):
        h = self.home("f")
        self.assertEqual(self.run_sh("--home", str(h)).returncode, 0)
        r = self.run_sh("--uninstall", "--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((h / ".local/share/agent-response-history").exists())
        backup = r.stdout.split("--rollback ")[1].split(")")[0]
        r = self.run_sh("--rollback", backup, "--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((h / ".local/share/agent-response-history/run.py").is_file())
        r = self.run_sh("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: install.py", r.stdout)

    def test_refuses_non_macos_before_any_change(self):
        linux = self.base / "bin-linux"
        linux.mkdir()
        stub(linux / "uname", "echo Linux")
        h = self.home("h")
        r = self.run_sh("--home", str(h), extra_path=[linux])
        self.assertEqual(r.returncode, 1)
        self.assertIn("requires macOS", r.stderr)
        self.assertFalse(h.exists())

    def test_requires_python_3_10(self):
        old = self.base / "bin-oldpy"
        old.mkdir()
        for client in ("claude", "codex"):
            stub(old / client, "exit 0")
        for name in ("python3", "python3.14", "python3.13", "python3.12", "python3.11", "python3.10"):
            old_python(old / name)  # shadow every name install.sh tries, whatever this machine has
        env = {"PATH": f"{old}:/usr/bin:/bin", "HOME": str(self.base / "not-home")}
        r = subprocess.run([str(ROOT / "install.sh"), "--home", str(self.home("i"))], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.10 or newer is required.", r.stderr)

    def test_spaces_and_quotes_in_paths_and_any_cwd(self):
        repo = self.base / "repo with 'quote' and space"
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "*.pyc"))
        h = self.base / "home with \"quotes\" and 'space'"
        r = self.run_sh("--home", str(h), repo=repo, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [self.good])
        r = self.run_sh("--home", str(h), repo=repo, cwd="/")
        self.assertIn("Already installed; nothing changed.", r.stdout)

    def test_install_py_failure_propagates(self):
        h = self.home("k")
        (h / ".claude/commands").mkdir(parents=True)
        (h / ".claude/commands/history-list.md").write_text("my own command")
        r = self.run_sh("--home", str(h))
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to overwrite", r.stderr)
        self.assertFalse((h / ".local/share").exists())

    def test_never_installs_codex_skills_or_prompts(self):
        h = self.home("l")
        self.assertEqual(self.run_sh("--home", str(h)).returncode, 0)
        for path in (".codex/skills", ".codex/prompts", ".agents"):
            self.assertFalse((h / path).exists(), path)

    def test_codex_interpreter_wins_unless_only_claude_is_installed(self):
        # (install.sh args, clients on PATH, expected Codex hook python, expected Claude hook python)
        cases = [(["--provider", "both"], "both", "B", "B"), (["--prov=both"], "both", "B", "B"),
                 (["--provider", "claude"], "both", "B", "A"), (["--pro", "claude"], "both", "B", "A"),
                 ([], "both", "B", "B"), ([], "codex", "B", "A"), ([], "claude", "B", "A")]
        for n, (args, bin, want_codex, want_claude) in enumerate(cases):
            with self.subTest(args=args, bin=bin):
                h = self.home(f"prefer-{n}")
                pythons = {"A": self.register(h, "claude", "python-A"), "B": self.register(h, "codex", "python-B")}
                codex_before = (h / ".codex/hooks.json").read_bytes()
                r = self.run_sh(*args, "--home", str(h), bin=bin)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual((h / ".codex/hooks.json").read_bytes(), codex_before)  # Codex trust input untouched
                self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [pythons[want_codex]])
                self.assertEqual(hook_python(h / ".claude/settings.json", "UserPromptExpansion"), [pythons[want_claude]])
                if want_claude == "A":
                    self.assertIn("Already installed; nothing changed.", r.stdout)

    def test_foreign_hooks_before_ours_are_ignored(self):
        h = self.home("foreign")
        ours = self.register(h, "both", "python-ours")
        other = self.base / "python-other"
        other.symlink_to(sys.executable)
        hook = shlex.quote(str(h / ".local/share/agent-response-history/integrations/hook.py"))
        commands = [f"{other} -I -B /elsewhere/other-tool/hook.py",
                    f"{other} {hook}",  # our hook path, not our structure
                    f"{other} -I -B /elsewhere/.local/share/agent-response-history/integrations/hook.py",  # another home
                    f"echo {hook}"]
        foreign = [{"hooks": [{"type": "command", "command": c}]} for c in [*commands, 123]]  # 123: not a string
        for config in (h / ".codex/hooks.json", h / ".claude/settings.json"):
            data = json.loads(config.read_text())
            data["hooks"] = {"PreToolUse": foreign, **{e: [*foreign, *g] for e, g in data["hooks"].items()}}
            config.write_text(json.dumps(data))
        before = (h / ".codex/hooks.json").read_bytes()
        r = self.run_sh("--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Already installed; nothing changed.", r.stdout)
        self.assertEqual((h / ".codex/hooks.json").read_bytes(), before)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [ours])

    def test_arguments_reach_install_py_exactly(self):
        probe = self.base / "probe"
        probe.mkdir()
        shutil.copy2(ROOT / "install.sh", probe / "install.sh")
        (probe / "install.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        odd = str(self.base / "it's a \"dir\" with spaces")
        explicit = [["--provider", "both"], ["--provider=codex"], ["--prov", "codex"], ["--p=claude"], ["--unin"],
                    ["--uninstall", "--home", odd], ["--roll", odd], [f"--roll={odd}"], ["--rollback", odd], ["-h"], ["--help"]]
        for args in explicit:
            with self.subTest(args=args):
                r = self.run_sh(*args, repo=probe)
                self.assertEqual(json.loads(r.stdout), args, r.stderr)  # never an added --provider
        tricky = str(self.base / "x --unin --roll y")  # option-like words inside one path are not options
        for args in (["--home", odd], ["--ho", odd], [f"--home={odd}"], ["--home", tricky]):
            with self.subTest(args=args):
                r = self.run_sh(*args, repo=probe)
                self.assertEqual(json.loads(r.stdout), ["--provider", "both", *args], r.stderr)

    def test_abbreviated_home_is_used_for_the_interpreter_lookup(self):
        h = self.home("abbrev")
        registered = self.register(h, "both", "python-registered")
        before = (h / ".codex/hooks.json").read_bytes()
        for flag in (["--ho", str(h)], [f"--hom={h}"]):
            with self.subTest(flag=flag):
                r = self.run_sh(*flag)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Already installed; nothing changed.", r.stdout)
                self.assertEqual((h / ".codex/hooks.json").read_bytes(), before)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [registered])

    def test_foreign_echo_hook_cannot_fake_an_install(self):
        # A foreign `echo <our hook path>` first in hooks.json must not become "the registered Python".
        h = self.home("echo")
        self.register(h, "codex", "python-ours")
        config = h / ".codex/hooks.json"
        data = json.loads(config.read_text())
        hook = shlex.quote(str(h / ".local/share/agent-response-history/integrations/hook.py"))
        data["hooks"]["UserPromptSubmit"].insert(0, {"hooks": [{"type": "command", "command": f"echo {hook}"}]})
        config.write_text(json.dumps(data))
        shutil.rmtree(h / ".local/share/agent-response-history")
        r = self.run_sh("--provider", "codex", "--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((h / ".local/share/agent-response-history/run.py").is_file(), r.stdout)

    def test_registered_non_python_is_rejected(self):
        # Exit status 0 is not enough: the interpreter must report Python >= 3.10.
        h = self.home("fake")
        self.register(h, "both", "python-real")
        fake = self.base / "fake-python"
        stub(fake, "exit 0")
        for config in (h / ".codex/hooks.json", h / ".claude/settings.json"):
            config.write_text(config.read_text().replace(str(self.base / "python-real"), str(fake)))
        shutil.rmtree(h / ".local/share/agent-response-history")
        r = self.run_sh("--home", str(h))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((h / ".local/share/agent-response-history/run.py").is_file(), r.stdout)
        self.assertEqual(hook_python(h / ".codex/hooks.json", "UserPromptSubmit"), [self.good])

    def test_relative_paths_keep_the_callers_meaning(self):
        # install.sh must resolve relative paths from the caller's directory, exactly like install.py.
        caller = self.base / "caller"
        caller.mkdir()
        r = self.run_sh("--home", "./test home", cwd=caller)
        self.assertEqual(r.returncode, 0, r.stderr)
        h = caller / "test home"
        self.assertTrue((h / ".local/share/agent-response-history/run.py").is_file())
        self.assertFalse((ROOT / "test home").exists())
        before = (h / ".codex/hooks.json").read_bytes()
        r = self.run_sh("--home", "test home", cwd=caller)  # the lookup also resolves from the caller
        self.assertIn("Already installed; nothing changed.", r.stdout)
        self.assertEqual((h / ".codex/hooks.json").read_bytes(), before)
        r = self.run_sh("--uninstall", "--home", "./test home", cwd=caller)
        self.assertEqual(r.returncode, 0, r.stderr)
        backup = Path(r.stdout.split("--rollback ")[1].split(")")[0])
        r = self.run_sh("--rollback", os.path.relpath(backup, caller), "--home", "./test home", cwd=caller)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((h / ".local/share/agent-response-history/run.py").is_file())

    def test_relative_config_dirs_keep_the_callers_meaning(self):
        caller = self.base / "caller-env"
        caller.mkdir()
        env = {"HOME": str(self.base / "env home"), "CLAUDE_CONFIG_DIR": "rel claude", "CODEX_HOME": "rel codex"}
        for attempt in range(2):
            r = self.run_sh(cwd=caller, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Already installed; nothing changed.", r.stdout)
        self.assertTrue((caller / "rel claude/settings.json").is_file())
        self.assertTrue((caller / "rel codex/hooks.json").is_file())
        self.assertFalse((ROOT / "rel claude").exists() or (ROOT / "rel codex").exists())


if __name__ == "__main__":
    unittest.main()
