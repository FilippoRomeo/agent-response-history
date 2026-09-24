import json
import shutil

from integrations.hook import handle
from response_history.clipboard import CopyResult
from response_history import archive, history_store, source
from response_history.adapters import claude, codex
from tests.test_history_store import Env


class HistoryUseTests(Env):
    def claude(self, name, args=""):
        return handle({"hook_event_name": "UserPromptExpansion", "session_id": self.claude_sid,
                       "transcript_path": str(self.claude_path), "cwd": str(self.repo),
                       "command_name": name, "command_args": args})["reason"]

    def codex(self, prompt):
        return handle({"hook_event_name": "UserPromptSubmit", "session_id": self.codex_sid,
                       "transcript_path": str(self.codex_path), "cwd": str(self.repo), "prompt": prompt})["reason"]

    def test_codex_family_routes(self):
        self.store("codex", "shader")
        self.assertTrue(self.codex("$history-use list").startswith("Current source: LIVE\n\n"))
        self.assertTrue(self.codex("$history-use shader").startswith("Current source: stored session shader ("))
        self.assertEqual(self.codex("$history-list").splitlines()[0], "Stored session: shader")
        self.assertIn("$history-use live switches back", self.codex("$history-use shader"))
        self.assertEqual(self.codex("$history-use live").splitlines()[0], "Current source: LIVE.")
        self.assertNotIn("Stored session", self.codex("$history-list"))
        self.assertIsNone(handle({"hook_event_name": "UserPromptSubmit", "session_id": self.codex_sid, "prompt": "$history-user x"}))

    def test_bare_is_list(self):  # Claude sends no arguments when Enter picks the command from the menu
        self.store("claude", "rocket")
        self.assertEqual(self.claude("history-use"), self.claude("history-use", "list"))

    def test_missing_original_still_selects(self):
        self.store("codex", "shader")
        self.codex_path.unlink()  # the original Codex session is gone from this machine
        reply = self.claude("history-use", "shader")
        self.assertIn("isn't on this machine any more", reply)
        self.assertNotIn("resume", reply)
        self.assertEqual(self.claude("history-list").splitlines()[0], "Stored session: shader")

    def test_available_original_shows_resume_but_never_runs_it(self):
        self.store("claude", "rocket")
        reply = self.claude("history-use", "rocket")
        self.assertIn(f"cd '{self.repo}' && claude --resume '{self.claude_sid}'", reply)  # subprocess.run is patched to fail

    def test_refusals_keep_the_current_source(self):
        folder = self.store("claude", "rocket")[0]
        self.store("claude", "broken")
        (self.project() / "broken" / "turns.jsonl").write_text("not json\n")
        self.claude("history-use", "rocket")
        for args, start in (("nope", "Nothing selected: no stored session named nope in this folder, in Claude Code's home store"),
                            ("broken", "Nothing selected: broken can't be used (its turns.jsonl is damaged)."),
                            ("a b", "Nothing selected. Usage: /history-use list | NAME | PATH | live"),
                            ('"unbalanced', "Nothing selected. Usage:"),
                            ("bad!name", "Nothing selected. Usage:"),
                            ("../rocket", "Nothing selected: ../rocket is not a stored session folder."),
                            ("~nobody/x", "Nothing selected: ~nobody paths aren't supported")):
            with self.subTest(args=args):
                self.assertTrue(self.claude("history-use", args).startswith(start))
                self.assertEqual(source.selected("claude", self.claude_sid)["path"], str(folder))

    def test_list_reports_a_broken_selection(self):
        folder = self.store("claude", "rocket")[0]
        self.claude("history-use", "rocket")
        folder.joinpath("meta.json").unlink()
        self.assertTrue(self.claude("history-use", "list").startswith(
            "Current source: stored session rocket, no longer usable (its meta.json can't be read)"))
        source._state_file("claude", self.claude_sid).write_text("{")
        self.assertTrue(self.claude("history-use", "list").startswith("Current source: unavailable. The selected stored session"))
        self.assertEqual(self.claude("history-use", "live").splitlines()[0], "Current source: LIVE.")  # the repair is reported
        self.assertEqual(self.claude("history-use", "live"), "Current source: LIVE (unchanged).")

    def new(self, provider="claude", cwd=None, **kw):
        sid, path = (self.claude_sid, self.claude_path) if provider == "claude" else (self.codex_sid, self.codex_path)
        return history_store.store_session(provider, sid, path, str(cwd or self.repo), **kw)[0]

    def rows(self, reply):
        return {line.split()[0]: line.split()[1] for line in reply.splitlines()[4:]}  # name -> where

    def test_discovery_combines_exactly_three_stores(self):
        self.new(name="here-one")
        self.new(name="home-one", where="home")
        self.new("codex", name="codex-home", where="home")  # the other client's home store
        self.new(name="parent-one", cwd=self.base / "work")  # a new-format store in a parent folder
        self.store("claude", "legacy-one")  # the v2.1 store for this project
        external = self.base / "elsewhere"
        self.new(name="external-one", where=str(external))
        reply = self.claude("history-use", "list")
        self.assertEqual(self.rows(reply), {"here-one": "here", "home-one": "home", "legacy-one": "v2.1"})
        self.assertEqual(self.rows(self.codex("$history-use LIST")), {"here-one": "here", "codex-home": "home", "legacy-one": "v2.1"})
        for hidden in ("codex-home", "parent-one", "external-one"):
            self.assertTrue(self.claude("history-use", hidden).startswith("Nothing selected: no stored session named"))
        # ...but every one of them is reachable by path
        for folder in (self.home / ".codex/agent-response-history/codex-home", external / "external-one"):
            self.assertTrue(self.claude("history-use", f"'{folder}'").startswith(f"Current source: stored session {folder.name} ("))

    def test_name_is_case_insensitive_and_legacy_names_resolve(self):
        self.store("claude", "Legacy-Rocket")
        self.assertTrue(self.claude("history-use", "legacy-rocket").startswith("Current source: stored session Legacy-Rocket ("))

    def test_relative_and_home_paths(self):
        folder = self.new(name="rel-one")
        self.assertTrue(self.claude("history-use", "./.agent-response-history/rel-one").startswith("Current source: stored session rel-one"))
        self.new(name="tilde-one", where="~")
        self.assertTrue(self.claude("history-use", "~/tilde-one").startswith("Current source: stored session tilde-one"))
        self.assertEqual(source.selected("claude", self.claude_sid)["path"], str(self.home / "tilde-one"))
        self.assertTrue(folder.is_dir())

    def test_duplicate_name_is_refused_with_paths(self):
        here = self.new(name="twin")
        home = history_store.home_root("claude")
        home.mkdir(mode=0o700)
        shutil.copytree(here, home / "twin")  # copied by hand: the same name in two discovered stores
        reply = self.claude("history-use", "twin")
        self.assertEqual(reply.splitlines(), ["Nothing selected: twin is ambiguous; stored sessions with that name are in:",
                                              f"  {here}", f"  {home / 'twin'}", "Choose one with /history-use PATH."])
        self.assertIsNone(source.selected("claude", self.claude_sid))
        self.assertTrue(self.claude("history-use", f"'{home / 'twin'}'").startswith("Current source: stored session twin"))

    def test_schema_1_is_listed_refused_for_d_and_keeps_a(self):
        old = self.project() / "old-one"
        old.mkdir(parents=True)
        (old / "meta.json").write_text(json.dumps({"schema_version": 1, "name": "old-one", "provider": "claude", "session_id": self.claude_sid,
                                                   "cwd": str(self.repo), "stored_at": "2026-09-01T10:00:00+00:00", "reply_count": 2,
                                                   "first_prompt_preview": "Old work"}))
        (old / "replies.md").write_text("# Session: old-one\n")
        listed = [line for line in self.claude("history-use", "list").splitlines() if line.startswith("old-one")]
        self.assertEqual(len(listed), 1)
        self.assertIn("v2.1", listed[0])
        self.assertIn("[v2.1 format] Old work", listed[0])
        reply = self.claude("history-use", "old-one")
        self.assertTrue(reply.startswith("Nothing selected: old-one can't be used (it was stored before v2.2"), reply)
        self.assertIn(f"claude --resume '{self.claude_sid}'", reply)  # A still offered
        self.assertIsNone(source.selected("claude", self.claude_sid))
        self.claude_path.unlink()
        self.assertNotIn("resume", self.claude("history-use", "old-one"))

    def test_folder_name_is_the_effective_name(self):
        same = self.new(name="login-fix")
        self.assertEqual(json.loads((same / "meta.json").read_text())["name"], same.name)  # normally they agree
        before = (same / "meta.json").read_bytes()
        renamed = same.with_name("login-fix-renamed")
        same.rename(renamed)  # renamed by hand after storing
        use = self.claude("history-use", f"'{renamed}'")
        self.assertTrue(use.startswith("Current source: stored session login-fix-renamed ("), use)
        self.assertEqual(self.claude("history-list").splitlines()[0], "Stored session: login-fix-renamed")
        copied = []
        copy = handle({"hook_event_name": "UserPromptExpansion", "session_id": self.claude_sid, "transcript_path": str(self.claude_path),
                       "cwd": str(self.repo), "command_name": "history-copy", "command_args": "1"},
                      lambda payload: copied.append(payload) or CopyResult("verified"))
        self.assertEqual((copy["reason"], len(copied)), ("Copied #1 from stored session login-fix-renamed", 1))
        listing = self.claude("history-use", "list").splitlines()
        self.assertEqual(listing[0], "Current source: stored session login-fix-renamed")
        self.assertEqual([line.split()[0] for line in listing[4:]], ["login-fix-renamed"])
        self.assertTrue(self.claude("history-use", "login-fix-renamed").startswith("Current source: stored session login-fix-renamed"))
        self.assertTrue(self.claude("history-use", "login-fix").startswith("Nothing selected: no stored session named login-fix"))
        self.assertEqual((renamed / "meta.json").read_bytes(), before)  # metadata never rewritten
        self.assertEqual(source.selected("claude", self.claude_sid)["name"], "login-fix-renamed")

    def test_cross_provider_d(self):
        self.new("codex", name="from-codex")  # a Codex archive in this folder, used from Claude
        self.assertIn("Codex", self.claude("history-use", "from-codex").splitlines()[0])
        listing = self.claude("history-list")
        self.assertEqual(listing.splitlines()[0], "Stored session: from-codex")
        self.assertEqual(len(listing.splitlines()), 3 + len(archive.load(self.repo / ".agent-response-history/from-codex")[0]))

    def test_family_commands_are_excluded_helpers(self):
        for name in ("history-list", "history-copy", "history-store", "history-use"):
            rows = [{"type": "user", "uuid": "u", "message": {"content": f"<command-name>/{name}</command-name>"}}]
            self.assertEqual(claude.parse(rows, "s")[0].excluded, "helper command", name)
            rows = [{"type": "response_item", "payload": {"type": "message", "role": "user",
                                                          "content": [{"type": "input_text", "text": f"${name} x"}]}}]
            self.assertEqual(codex.parse(rows, "s")[0].excluded, "helper command", name)

    def test_state_record_is_the_only_write(self):
        self.store("claude", "rocket")
        before = sorted(p for p in self.home.rglob("*"))
        self.claude("history-use", "rocket")
        after = sorted(p for p in self.home.rglob("*"))
        new = [p for p in after if p not in before and p.is_file()]
        self.assertEqual(new, [source._state_file("claude", self.claude_sid)])
        self.assertEqual(json.loads(new[0].read_text())["name"], "rocket")
