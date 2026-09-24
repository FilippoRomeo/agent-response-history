import json
import shutil
import stat
import uuid

from response_history import archive, source
from response_history.model import UserError
from integrations.hook import handle
from tests.test_history_store import Env


class SourceTests(Env):
    def setUp(self):
        super().setUp()
        self.rocket = self.store("claude", "rocket")[0]
        self.shader = self.store("codex", "shader")[0]
        self.other = str(uuid.uuid4())

    def load(self, provider="claude", session=None):
        path = self.claude_path if provider == "claude" else self.codex_path
        return source.load(provider, session or (self.claude_sid if provider == "claude" else self.codex_sid), path)

    def test_default_is_live(self):
        turns, label = self.load()
        self.assertIsNone(label)
        self.assertEqual([t.text for t in turns], [t.text for t in source.load_turns("claude", self.claude_path, self.claude_sid)])

    def test_selection_is_per_native_session(self):
        source.select_archive("claude", self.claude_sid, self.shader)  # a Claude session may select a Codex archive
        self.assertEqual(self.load()[1], "shader")
        self.assertIsNone(source.selected("claude", self.other))       # another Claude session: still LIVE
        self.assertIsNone(source.selected("codex", self.claude_sid))   # same ID under the other client: still LIVE
        self.assertIsNone(self.load("codex")[1])

    def test_archive_turns_replace_live_turns_exactly(self):
        source.select_archive("claude", self.claude_sid, self.shader)
        turns, label = self.load()
        self.assertEqual([t.text for t in turns], [t.text for t in archive.load(self.shader)[0]])
        self.assertTrue(all(t.selectable for t in turns))

    def test_selection_persists_for_the_same_session_until_cleared(self):
        source.select_archive("claude", self.claude_sid, self.rocket)
        self.assertEqual(self.load()[1], "rocket")  # e.g. after resuming this same session later (decision D1)
        source.select_live("claude", self.claude_sid)
        self.assertIsNone(self.load()[1])
        source.select_live("claude", self.claude_sid)  # clearing twice is harmless

    def test_missing_or_damaged_archive_fails_closed(self):
        source.select_archive("claude", self.claude_sid, self.rocket)
        (self.rocket / archive.TURNS).write_text("{broken\n")
        with self.assertRaises(UserError) as cm:
            self.load()
        self.assertIn("no longer available", str(cm.exception))
        shutil.rmtree(self.rocket)
        with self.assertRaises(UserError) as cm:
            self.load()
        self.assertEqual(str(cm.exception), "The selected stored session is no longer available (its folder no longer exists). "
                                             "Choose another stored session or switch back to the live session.")
        self.assertIn("switch back to the live session", str(cm.exception))

    def test_refused_archives_are_never_recorded(self):
        meta = json.loads((self.rocket / "meta.json").read_text()); meta["schema_version"] = 1
        (self.rocket / "meta.json").write_text(json.dumps(meta))
        with self.assertRaises(UserError):
            source.select_archive("claude", self.claude_sid, self.rocket)
        self.assertIsNone(source.selected("claude", self.claude_sid))
        for bad in ("../../etc", "not-a-uuid", ""):
            with self.assertRaises(UserError):
                source.select_archive("claude", bad, self.shader)
        with self.assertRaises(UserError):
            source.select_archive("other-client", self.claude_sid, self.shader)

    def test_unreadable_selection_record_fails_closed(self):
        source.select_archive("claude", self.claude_sid, self.shader)
        source._state_file("claude", self.claude_sid).write_text("{not json")
        with self.assertRaises(UserError) as cm:
            self.load()
        self.assertIn("selection record", str(cm.exception))

    def test_fresh_session_without_transcript_uses_the_selected_archive(self):
        fresh, missing = str(uuid.uuid4()), self.base / "not-written-yet.jsonl"
        event = {"hook_event_name": "UserPromptExpansion", "session_id": fresh, "transcript_path": str(missing),
                 "cwd": str(self.repo), "command_name": "history-list", "command_args": ""}
        self.assertEqual(handle(event)["reason"], "No responses yet.")  # LIVE, nothing written yet
        source.select_archive("claude", fresh, self.shader)
        self.assertTrue(handle(event)["reason"].startswith("Stored session: shader\nNo.  Preview"))

    def test_state_is_private_and_outside_the_installer(self):
        source.select_archive("claude", self.claude_sid, self.shader)
        f = source._state_file("claude", self.claude_sid)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        for d in (f.parent, source.state_root()):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        self.assertNotIn("share/agent-response-history/", str(f))
        self.assertEqual([p.name for p in f.parent.iterdir()], [f.name])  # no temp files left
