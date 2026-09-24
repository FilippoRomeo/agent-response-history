"""history-store: destinations, names, collisions, metadata, Git safety."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.hook import handle
from response_history import archive, history_store, session
from tests.test_history_store import Env, LEAKS, NOW, mode, write_rows

STORE = ".agent-response-history"


class StoreEnv(Env):
    def add(self, path, *rows):
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def claude_title(self, kind, value):
        key = "customTitle" if kind == "custom-title" else "aiTitle"
        self.add(self.claude_path, {"type": kind, key: value, "sessionId": self.claude_sid})

    def codex_name(self, value, sid=None):
        self.add(self.home / ".codex/session_index.jsonl", {"id": sid or self.codex_sid, "thread_name": value, "updated_at": "x"})

    def claude(self, args, cwd=None, name="history-store"):
        return handle({"hook_event_name": "UserPromptExpansion", "session_id": self.claude_sid,
                       "transcript_path": str(self.claude_path), "cwd": str(cwd or self.repo),
                       "command_name": name, "command_args": args})["reason"]

    def codex(self, prompt, cwd=None):
        return handle({"hook_event_name": "UserPromptSubmit", "session_id": self.codex_sid,
                       "transcript_path": str(self.codex_path), "cwd": str(cwd or self.repo), "prompt": prompt})["reason"]

    def new(self, provider="claude", cwd=None, **kw):
        sid, path = (self.claude_sid, self.claude_path) if provider == "claude" else (self.codex_sid, self.codex_path)
        return history_store.store_session(provider, sid, path, str(cwd or self.repo), now=NOW, **kw)


class TitleTests(StoreEnv):
    def test_claude_precedence(self):
        self.assertEqual(session.title("claude", self.claude_sid, self.claude_path), (None, None))
        self.claude_title("ai-title", "Auto one")
        self.claude_title("ai-title", "Auto two")
        self.assertEqual(session.title("claude", self.claude_sid, self.claude_path), ("Auto two", "ai-title"))
        self.claude_title("custom-title", "Mine")
        self.claude_title("ai-title", "Auto three")  # a later automatic title never beats /rename
        self.assertEqual(session.title("claude", self.claude_sid, self.claude_path), ("Mine", "custom-title"))
        self.claude_title("custom-title", "Mine again")
        self.assertEqual(session.title("claude", self.claude_sid, self.claude_path), ("Mine again", "custom-title"))

    def test_claude_empty_custom_title_clears_the_name(self):
        self.claude_title("custom-title", "old-name")
        self.claude_title("ai-title", "automatic-name")
        self.claude_title("custom-title", "")  # the last custom-title record wins; empty means cleared
        self.assertEqual(session.title("claude", self.claude_sid, self.claude_path), ("automatic-name", "ai-title"))
        self.assertEqual(self.new()[0].name, "automatic-name")  # old-name is never resurrected

    def test_claude_cleared_name_without_ai_title_falls_back(self):
        self.claude_title("custom-title", "old-name")
        self.claude_title("custom-title", "")
        self.assertEqual(session.title("claude", self.claude_sid, self.claude_path), (None, None))
        self.assertEqual(self.new()[3]["name_source"], "fallback")

    def test_codex_session_index(self):
        self.assertEqual(session.title("codex", self.codex_sid, self.codex_path), (None, None))
        other = "01a0d51d-4535-7732-869e-d75dd8a207d6"
        self.codex_name("Other thread", other)
        self.codex_name("Generated name")
        (self.home / ".codex/session_index.jsonl").open("a").write("{not json " + self.codex_sid + "\n")
        self.codex_name("Renamed")
        self.codex_name("  ")
        self.assertEqual(session.title("codex", self.codex_sid, self.codex_path), ("Renamed", "codex-thread-name"))

    def test_codex_never_uses_threads_title(self):
        (self.home / ".codex/state_5.sqlite").write_text("not read")  # threads.title is the first prompt
        self.assertEqual(session.title("codex", self.codex_sid, self.codex_path), (None, None))


class SlugTests(unittest.TestCase):
    def test_slugs(self):
        cases = {
            "Fix login form reload (Safari)": "fix-login-form-reload-safari",
            "Café ☕ menu — v2": "cafe-menu-v2",
            "Ärger über Größe": "arger-uber-gro-e",  # ß has no decomposition
            "ﬁle ｆｕｌｌｗｉｄｔｈ": "file-fullwidth",
            "東京の天気": "",
            "   ---   ": "",
            "word " * 30: "word-word-word-word-word-word-word-word-word",
            "x" * 80: "x" * 48,
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = history_store.slug(title)
                self.assertEqual(result, expected)
                self.assertRegex(result or "a", r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(result), 48)


class DestinationTests(StoreEnv):
    def test_default_is_the_exact_cwd_never_a_parent(self):
        deep = self.repo / "src" / "deep"
        folder, count, durable, meta = self.new(cwd=deep)
        self.assertEqual(folder.parent, deep / STORE)
        self.assertFalse((self.repo / STORE).exists())
        self.assertEqual(meta["cwd"], str(deep))
        self.assertTrue(durable)

    def test_non_git_cwd(self):
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(self.new(cwd=plain)[0].parent, plain / STORE)

    def test_home_roots_and_overrides(self):
        for token in ("home", "HOME", "Home"):
            self.assertEqual(self.new(where=token)[0].parent, self.home / ".claude/agent-response-history")
        self.assertEqual(self.new("codex", where="home")[0].parent, self.home / ".codex/agent-response-history")
        other = self.base / "other-config"
        other.mkdir()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.assertEqual(self.new(where="home")[0].parent, other / "agent-response-history")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):  # empty means unset
            self.assertEqual(history_store.home_root("claude"), self.home / ".claude/agent-response-history")
        self.assertEqual(self.new(where="./home")[0].parent, self.repo / "home")  # a folder called home

    def test_explicit_paths(self):
        existing = self.base / "chats"
        existing.mkdir(mode=0o755)
        self.assertEqual(self.new(where=str(existing))[0].parent, existing)
        self.assertEqual(mode(existing), 0o755)  # an existing folder is used as it is
        fresh = self.base / "fresh"
        self.assertEqual(self.new(where=str(fresh))[0].parent, fresh)
        self.assertEqual(mode(fresh), 0o700)  # created: one level, private
        self.assertEqual(self.new(where="rel")[0].parent, self.repo / "rel")  # relative to the session's folder
        self.assertEqual(self.new(where="~")[0].parent, self.home)
        (self.home / "notes").mkdir()
        self.assertEqual(self.new(where="~/notes")[0].parent, self.home / "notes")

    def test_explicit_path_refusals_write_nothing(self):
        (self.base / "a-file").write_text("x")
        before = sorted(self.base.rglob("*"))
        for where, message in ((str(self.base / "missing/child"), "the folder containing"),
                               (str(self.base / "a-file"), "is not a folder"),
                               ("~root/x", "~root paths aren't supported")):
            with self.subTest(where=where):
                with self.assertRaises(history_store.HistoryError) as caught:
                    self.new(where=where)
                self.assertIn(message, str(caught.exception))
        self.assertEqual(sorted(self.base.rglob("*")), before)

    def test_symlink_is_resolved(self):
        target = self.base / "real-target"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target)
        reply = self.claude(f"'{link}'")
        folder = next(target.iterdir())
        self.assertIn(str(folder), reply)
        self.assertNotIn(str(link), reply)
        self.assertFalse(folder.is_symlink())

    def test_spaces_and_quotes(self):
        odd = self.base / "it's a \"dir\""
        odd.mkdir()
        reply = self.claude(f"""'{str(odd).replace("'", "'\"'\"'")}' --note "a 'quoted' note" --name odd-one""")
        self.assertTrue((odd / "odd-one").is_dir(), reply)
        meta = json.loads((odd / "odd-one/meta.json").read_text())
        self.assertEqual(meta["note"], "a 'quoted' note")
        use = reply.splitlines()[-1].split("with: ", 1)[1]
        self.assertTrue(self.claude(use.split(" ", 1)[1], name="history-use").startswith("Current source: stored session odd-one"))


class NameTests(StoreEnv):
    def test_title_names_the_archive_and_is_kept_exactly(self):
        self.claude_title("custom-title", "Fix: login  form\n(Safari) — ünïcode")
        folder, _, _, meta = self.new()
        self.assertEqual(folder.name, "fix-login-form-safari-unicode")
        stored = json.loads((folder / "meta.json").read_text())
        self.assertEqual((stored["name"], stored["title"], stored["title_source"], stored["name_source"]),
                         ("fix-login-form-safari-unicode", "Fix: login form (Safari) — ünïcode", "custom-title", "title"))

    def test_codex_title_source(self):
        self.codex_name("Boot screen pacing")
        folder, _, _, meta = self.new("codex")
        self.assertEqual((folder.name, meta["title_source"]), ("boot-screen-pacing", "codex-thread-name"))

    def test_fallback_without_a_title(self):
        folder, _, _, meta = self.new()
        self.assertEqual(folder.name, f"claude-{NOW.astimezone():%Y-%m-%d}-{self.claude_sid[:8]}")
        self.assertEqual((meta["title"], meta["title_source"], meta["name_source"]), (None, None, "fallback"))
        self.claude_title("ai-title", "東京の天気")  # nothing usable in ASCII: fallback, title kept
        folder, _, _, meta = self.new()
        self.assertTrue(folder.name.startswith("claude-"))
        self.assertEqual((meta["title"], meta["name_source"]), ("東京の天気", "fallback"))

    def test_generated_collisions_and_reserved_names_get_suffixes(self):
        self.claude_title("ai-title", "Rocket launch")
        names = [self.new()[0].name for _ in range(3)]
        self.assertEqual(names, ["rocket-launch", "rocket-launch-2", "rocket-launch-3"])
        for title, expected in (("List", "list-2"), ("LIVE", "live-2")):
            self.claude_title("custom-title", title)
            self.assertEqual(self.new()[0].name, expected)

    def test_generated_names_avoid_every_discovered_store(self):
        self.claude_title("ai-title", "Rocket launch")
        self.new(where="home")
        self.assertEqual(self.new()[0].name, "rocket-launch-2")  # home already has rocket-launch

    def test_explicit_name_is_authoritative(self):
        self.assertEqual(self.new(name="Login-Fix")[0].name, "Login-Fix")
        for bad in ("list", "LIST", "Live", "LIVE"):
            with self.subTest(name=bad), self.assertRaises(history_store.HistoryError) as caught:
                self.new(name=bad)
            self.assertIn("is reserved", str(caught.exception))
        for taken, where in (("login-fix", None), ("Login-Fix", "home")):  # any case, target or discovered store
            with self.subTest(name=taken, where=where), self.assertRaises(history_store.HistoryError) as caught:
                self.new(name=taken, where=where)
            self.assertIn("already exists; choose another --name", str(caught.exception))
        with self.assertRaises(history_store.HistoryError):
            self.new(name="has space")

    def test_old_store_refuses_reserved_names(self):
        for bad in ("list", "LIVE", "Live"):
            with self.subTest(name=bad), self.assertRaises(history_store.HistoryError):
                self.store("claude", bad)

    def test_concurrent_publish(self):
        self.claude_title("ai-title", "Rocket launch")
        real, claims = history_store._publish, []

        def racing(src, dst):
            if len(claims) < 2:
                claims.append(dst.name)
                dst.mkdir()  # another writer claims the name after every check
            real(src, dst)
        with patch.object(history_store, "_publish", racing):
            folder = self.new()[0]
        self.assertEqual(claims, ["rocket-launch", "rocket-launch-2"])
        self.assertEqual(folder.name, "rocket-launch-3")  # -1 and -2 were each claimed first
        claims.clear()
        with patch.object(history_store, "_publish", racing), self.assertRaises(history_store.HistoryError):
            self.new(name="exact")
        root = self.repo / STORE
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["exact", "rocket-launch", "rocket-launch-2", "rocket-launch-3"])
        self.assertEqual(list((root / "exact").iterdir()), [])  # the racer's folder, untouched
        self.assertFalse([p for p in root.iterdir() if p.name.startswith(".tmp-")])


class ArchiveTests(StoreEnv):
    def test_schema_files_permissions_and_privacy(self):
        folder, count, _, _ = self.new(note="first try")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [".gitignore", "meta.json", "replies.md", "turns.jsonl"])
        self.assertEqual((folder / ".gitignore").read_text(), "*\n")
        self.assertEqual(mode(folder), 0o700)
        self.assertEqual(mode(folder.parent), 0o700)
        for f in folder.iterdir():
            self.assertEqual(mode(f), 0o600, f.name)
        turns, meta = archive.load(folder)
        self.assertEqual((len(turns), meta["schema_version"], meta["note"]), (count, 2, "first try"))
        text = "".join(f.read_text() for f in folder.iterdir()) + self.claude("--name leak-check")
        for leak in LEAKS[:-2]:
            self.assertNotIn(leak, text)

    def test_default_store_writes_nothing_in_client_homes(self):
        before = sorted(p for p in self.home.rglob("*"))
        self.new()
        self.assertEqual(sorted(p for p in self.home.rglob("*")), before)

    def test_reply_and_usage(self):
        self.claude_title("ai-title", "Rocket launch")
        reply = self.claude("")
        folder = self.repo / STORE / "rocket-launch"
        self.assertEqual(reply.splitlines(), [
            'Stored rocket-launch (2 replies), named after "Rocket launch".', str(folder),
            f"Use it later with: /history-use rocket-launch (in this folder), or from anywhere: /history-use '{folder}'"])
        self.assertEqual(self.codex("$history-store home --name shared").splitlines()[-1], "Use it later with: $history-use shared")
        usage = "Nothing stored. Usage: /history-store [home | PATH] [--note TEXT] [--name NAME]"
        for bad in ('"unbalanced', "--nope x", "--name", "a b", "''", "--note", "-h", "--nam x"):
            with self.subTest(args=bad):
                self.assertEqual(self.claude(bad), usage)

    def test_errors_never_show_private_details(self):
        with patch.object(history_store, "_write", side_effect=OSError("/private/secret path")):
            reply = self.claude("--name x")
        self.assertEqual(reply, "Stored-session error: OSError")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitSafetyTests(unittest.TestCase):
    def test_git_never_picks_up_an_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            env = {**os.environ, "HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1"}
            git = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout
            git("init", "-q")
            (repo / "code.txt").write_text("x")
            sid = "00000000-0000-4000-8000-0000000000aa"
            transcript = write_rows(repo / "t.jsonl", [
                {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid, "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid,
                 "message": {"id": "m1", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "PRIVATE"}]}}])
            with patch.dict(os.environ, {"HOME": tmp}):
                history_store.store_session("claude", sid, transcript, str(repo), name="kept-out")
            self.assertTrue((repo / STORE / "kept-out/turns.jsonl").exists())
            self.assertNotIn(STORE, git("status", "--porcelain", "--untracked-files=all"))
            git("add", "-A")
            self.assertEqual(sorted(git("diff", "--cached", "--name-only").split()), ["code.txt", "t.jsonl"])


if __name__ == "__main__":
    unittest.main()
