"""Live JSONL transcripts split on "\\n" only, never on the other characters str.splitlines() treats as line ends."""
import json
import uuid

from integrations.hook import handle
from response_history import archive, history_store
from response_history.adapters.common import records
from response_history.clipboard import CopyResult
from response_history.model import TranscriptError
from response_history.transcript import load_turns
from tests.test_history_store import Env, claude_rows, codex_rows

SEPARATORS = "  \x85\x0b\x0c\x1c\x1d\x1e"  # every one is a str.splitlines() boundary
RAW = "  \x85"  # JSON leaves these raw; it must escape the C0 ones (VT, FF, U+001C-U+001E)
VISIBLE = "Line one line two end"


def write(path, rows, end="\n"):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + end for r in rows), encoding="utf-8")
    return path


def claude_fixture(session):
    rows = claude_rows(session)
    rows[1]["message"]["content"][2]["input"]["cmd"] = "TOOL" + SEPARATORS  # tool_use input
    rows[2]["message"]["content"][0]["content"] = "RESULT" + SEPARATORS  # tool_result
    rows[3]["message"]["content"][0]["text"] = VISIBLE  # was "Fixed it."
    return rows


def codex_fixture(session):
    rows = codex_rows(session)
    rows[6]["payload"]["arguments"] = "TOOL" + SEPARATORS  # function_call
    rows[7]["payload"]["output"] = "RESULT" + SEPARATORS  # function_call_output
    rows[8]["payload"]["content"][0]["text"] = VISIBLE  # was "Pipeline fixed."
    rows[9]["payload"]["last_agent_message"] = VISIBLE
    return rows


class SeparatorTests(Env):
    def setUp(self):
        super().setUp()
        self.rows = {"claude": claude_fixture(self.claude_sid), "codex": codex_fixture(self.codex_sid)}
        write(self.claude_path, self.rows["claude"])
        write(self.codex_path, self.rows["codex"])
        self.cases = (("claude", self.claude_sid, self.claude_path, "Looking.\n\n" + VISIBLE),
                      ("codex", self.codex_sid, self.codex_path, "Checking.\n\n" + VISIBLE))

    def hook(self, provider, name, args="", session=None, transport=None):
        event = {"session_id": session or (self.claude_sid if provider == "claude" else self.codex_sid),
                 "transcript_path": str(self.claude_path if provider == "claude" else self.codex_path), "cwd": str(self.repo)}
        if provider == "claude":
            event.update(hook_event_name="UserPromptExpansion", command_name=name, command_args=args)
        else:
            event.update(hook_event_name="UserPromptSubmit", prompt=f"${name} {args}".strip())
        return handle(event, transport)["reason"]

    def test_fixture_holds_raw_separators(self):
        for provider, _, path, _ in self.cases:
            with self.subTest(provider=provider):
                text = path.read_text(encoding="utf-8")
                for ch in RAW:
                    self.assertIn(ch, text)  # physically present, not \\u-escaped
                self.assertGreater(len(text.splitlines()), text.count("\n"))  # the old reader would split records
                self.assertEqual(records(path), self.rows[provider])  # all eight survive, raw or escaped

    def test_visible_text_is_preserved_exactly(self):
        for provider, sid, path, first in self.cases:
            with self.subTest(provider=provider):
                turns = load_turns(provider, path, sid)
                self.assertEqual([t.text for t in turns][0], first)
                self.assertEqual(len(turns), 2)

    def test_list_copy_store(self):
        for provider, _, _, first in self.cases:
            with self.subTest(provider=provider):
                listing = self.hook(provider, "history-list")
                self.assertEqual([line.split()[0] for line in listing.splitlines()[2:]], ["#1", "#2"])
                copied = []
                reply = self.hook(provider, "history-copy", "1", transport=lambda p: copied.append(p) or CopyResult("verified"))
                self.assertEqual((reply, copied), ("Copied #1", [first]))
                name = f"sep-{provider}"
                self.assertEqual(self.hook(provider, "history-store", f"--name {name}").splitlines()[0], f"Stored {name} (2 replies).")
                turns, _ = archive.load(history_store.here_root(str(self.repo)) / name)
                self.assertEqual(turns[0].text, first)

    def test_wrong_session_is_still_refused(self):
        for provider, _, path, _ in self.cases:
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(TranscriptError, "^Session identity mismatch$"):
                    load_turns(provider, path, str(uuid.uuid4()))
                other = str(uuid.uuid4())
                self.assertIn("Session identity mismatch", self.hook(provider, "history-list", session=other))
                self.assertEqual(self.hook(provider, "history-store", "--name wrong", session=other),
                                 "Stored-session error: Session identity mismatch")

    def test_invalid_line_reports_physical_line_number(self):
        path = write(self.base / "bad.jsonl", self.rows["claude"][:2])
        path.write_text(path.read_text(encoding="utf-8") + "{bad}\n" + json.dumps({"x": RAW}, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(TranscriptError, "^Invalid transcript JSON at line 3$"):
            records(path)

    def test_unfinished_tail_only_when_unterminated(self):
        path = write(self.base / "tail.jsonl", self.rows["codex"][:3])
        complete = path.read_text(encoding="utf-8")
        for tail in ('{"unfinished":"a' + RAW, '{"unfinished":', '{"x":"' + RAW):
            with self.subTest(tail=tail):
                path.write_text(complete + tail, encoding="utf-8")
                self.assertEqual(records(path), self.rows["codex"][:3])
                path.write_text(complete + tail + "\n", encoding="utf-8")  # terminated: no longer a tail
                with self.assertRaisesRegex(TranscriptError, "^Invalid transcript JSON at line 4$"):
                    records(path)
        path.write_text(complete + json.dumps({"x": RAW}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(records(path)[-1], {"x": RAW})  # valid final JSON needs no newline

    def test_crlf(self):
        for provider, sid, path, first in self.cases:
            with self.subTest(provider=provider):
                write(path, self.rows[provider], "\r\n")
                self.assertEqual(records(path), self.rows[provider])
                self.assertEqual(load_turns(provider, path, sid)[0].text, first)

    def test_store_reports_safe_transcript_error(self):
        text = self.claude_path.read_text(encoding="utf-8").split("\n")
        text[2] = '{"PRIVATE_BODY": bad}'
        self.claude_path.write_text("\n".join(text), encoding="utf-8")
        reply = self.hook("claude", "history-store", "--name broken")
        self.assertEqual(reply, "Stored-session error: Invalid transcript JSON at line 3")
