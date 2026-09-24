import json
import os
import stat

from response_history import archive, history_store
from response_history.model import Turn, UserError
from tests.test_history_store import Env, LEAKS

TRICKY = [
    "## User\n\nnot a real heading\n\n## Assistant\n\n# Session: fake",
    "```py\nprint('fenced')\n```\n\n\n\nthree blank lines above",
    "Unicode ✓ é 漢字 🙂 and U+2028 [ ] and U+2029 [ ] and NEL [\x85] and tab [\t]",
    "  leading spaces and a trailing newline\n",
    "\"quotes\" and 'single' and back\\slash",
]


class ArchiveTests(Env):
    def stored(self, name="rocket", provider="claude"):
        return self.store(provider, name)[0]

    def test_round_trip_is_exact_for_tricky_replies(self):
        turns = [Turn("claude", "s", parts=[r], state="complete", prompt=f"prompt {i} ## User") for i, r in enumerate(TRICKY)]
        folder = self.base / "manual"
        folder.mkdir()
        (folder / "meta.json").write_text(json.dumps({"schema_version": 2, "provider": "claude", "reply_count": len(turns)}))
        (folder / archive.TURNS).write_text(archive.dumps(turns), encoding="utf-8")
        loaded, meta = archive.load(folder)
        self.assertEqual([t.text for t in loaded], TRICKY)  # exact text back, including the U+2028 line
        self.assertEqual([t.prompt for t in loaded], [t.prompt for t in turns])
        self.assertTrue(all(t.selectable for t in loaded))

    def test_store_writes_the_same_turns_the_session_had(self):
        for provider, name in (("claude", "rocket"), ("codex", "shader")):
            folder = self.stored(name, provider)
            loaded, meta = archive.load(folder)
            live = history_store.load_turns(provider, self.claude_path if provider == "claude" else self.codex_path,
                                            self.claude_sid if provider == "claude" else self.codex_sid)
            self.assertEqual([(t.prompt, t.text) for t in loaded], [(t.prompt, t.text) for t in live])
            self.assertEqual((meta["schema_version"], meta["reply_count"]), (2, len(live)))
            self.assertEqual(stat.S_IMODE((folder / archive.TURNS).stat().st_mode), 0o600)

    def test_privacy_nothing_filtered_leaks_into_turns(self):
        for provider, name in (("claude", "rocket"), ("codex", "shader")):
            text = (self.stored(name, provider) / archive.TURNS).read_text(encoding="utf-8")
            for leak in LEAKS:
                self.assertNotIn(leak, text, (provider, leak))

    def test_fails_closed(self):
        folder = self.stored()
        good = {f: (folder / f).read_bytes() for f in ("meta.json", archive.TURNS)}

        def refused(fragment):
            with self.assertRaises(UserError) as cm:
                archive.load(folder)
            self.assertIn(fragment, str(cm.exception))
            for f, data in good.items():
                (folder / f).write_bytes(data)

        (folder / archive.TURNS).unlink(); refused("can't be read")
        (folder / archive.TURNS).write_text(good[archive.TURNS].decode().rsplit("\n", 2)[0] + "\n"); refused("doesn't match")
        (folder / archive.TURNS).write_text("{not json\n"); refused("damaged")
        (folder / archive.TURNS).write_text('{"prompt": 1, "response": "x"}\n'); refused("damaged")
        meta = json.loads(good["meta.json"]); meta["schema_version"] = 1
        (folder / "meta.json").write_text(json.dumps(meta)); refused("before v2.2")
        meta["schema_version"] = 9
        (folder / "meta.json").write_text(json.dumps(meta)); refused("unknown format")
        (folder / "meta.json").write_text("[]"); refused("not valid")
        os.chmod(folder, 0o700)
        self.assertEqual(len(archive.load(folder)[0]), 2)  # restored archive loads again
