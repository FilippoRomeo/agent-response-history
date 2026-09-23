import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from response_history.adapters import codex, claude
from response_history.adapters.common import records
from response_history.clipboard import CopyResult, copy_verified
from response_history.cli import main, preview
from response_history.model import TranscriptError
from response_history.selection import select

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(os.environ.get("RESPONSE_HISTORY_TEST_TMP", tempfile.gettempdir())) / "response_history_tests"
TMP.mkdir(parents=True, exist_ok=True)


def c_event(kind, **values):
    return {"type": "event_msg", "payload": {"type": kind, **values}}


def c_msg(text, phase="commentary", role="assistant", **values):
    return {"type": "response_item", "payload": {"type": "message", "role": role, "phase": phase,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}], **values}}


def a_user(text, **values):
    return {"type": "user", "sessionId": "synthetic-session", "uuid": "synthetic-user", "isSidechain": False,
            "message": {"content": text}, **values}


def a_msg(parts, stop=None, **values):
    return {"type": "assistant", "sessionId": "synthetic-session", "uuid": "synthetic-assistant",
            "parentUuid": "synthetic-user", "isSidechain": False,
            "message": {"content": parts, "stop_reason": stop}, **values}


def text(s):
    return {"type": "text", "text": s}


def fixture(name, rows):
    path = TMP / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


class SelectionTests(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(select(None, 3), [3])
        self.assertEqual(select("-10", 3), [1, 2, 3])
        self.assertEqual(select("-2", 3), [2, 3])
        self.assertEqual(select("#2,1-2,#2", 3), [2, 1, 2, 2])
        for raw in ("0", "-0", "-11", "2-1", "1,4", "2-", "-1,2", "banana", "#0"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                select(raw, 3)


class CodexTests(unittest.TestCase):
    def test_live_0156_multiple_final_responses_in_one_task(self):
        # Sanitized shape of the iTerm transcript: final, new input, final,
        # with one task_started/task_complete pair around both responses.
        first_user = c_msg("first input", role="user")
        first_user["payload"]["content"].append({"type": "input_text", "text": "more input"})
        rows = [c_event("task_started", turn_id="live-task"),
                first_user,
                c_event("item_completed", item={"type": "UserMessage", "id": "u1"}, turn_id="live-task"),
                c_event("item_completed", item={"type": "AgentMessage", "id": "m1", "phase": "final_answer", "content": [{"type": "Text", "text": "first response"}]}, turn_id="live-task"),
                c_msg("first response", "final_answer", id="m1"),
                c_event("token_count"),
                c_msg("second input", role="user"),
                c_event("item_completed", item={"type": "UserMessage", "id": "u2"}, turn_id="live-task"),
                c_event("item_completed", item={"type": "AgentMessage", "id": "m2", "phase": "final_answer", "content": [{"type": "Text", "text": "second response"}]}, turn_id="live-task"),
                c_msg("second response", "final_answer", id="m2"),
                c_event("task_complete", turn_id="live-task", last_agent_message="second response")]
        self.assertEqual([t.text for t in codex.parse(rows, "sanitized") if t.selectable],
                         ["first response", "second response"])

    def test_consecutive_implicit_turns(self):
        rows = [c_msg("first input", role="user"), c_msg("first", "final_answer"),
                c_msg("second input", role="user"), c_msg("second", "final_answer"),
                c_msg("third input", role="user"), c_msg("third", "final_answer")]
        self.assertEqual([t.text for t in codex.parse(rows, "sanitized") if t.selectable],
                         ["first", "second", "third"])

    def test_tool_result_and_abort_do_not_create_implicit_responses(self):
        tool_result = {"type": "response_item", "payload": {"type": "message", "role": "user",
                       "content": [{"type": "tool_result", "text": "generated"}]}}
        rows = [c_event("task_started", turn_id="a"), c_msg("first", "final_answer"),
                tool_result, c_msg("second", "final_answer")]
        with self.assertRaises(TranscriptError):
            codex.parse(rows, "synthetic")
        aborted = [c_event("task_started", turn_id="a"), c_msg("draft", "final_answer"),
                   c_event("turn_aborted")]
        self.assertFalse(any(t.selectable for t in codex.parse(aborted, "synthetic")))

    def test_final_answer_and_fallback(self):
        rows = [c_event("task_started", turn_id="a"), c_msg("progress"), c_msg("done", "final_answer"),
                c_event("task_complete", turn_id="a", last_agent_message="done")]
        self.assertEqual([t.text for t in codex.parse(rows, "synthetic") if t.selectable], ["progress\n\ndone"])
        self.assertEqual(codex.parse([c_event("task_started"), c_msg("same"), c_event("task_complete", last_agent_message="same")], "s")[0].text, "same\n\nsame")
        self.assertEqual(codex.parse([c_event("task_started"), c_msg("legacy", "final"), c_event("task_complete", last_agent_message="legacy")], "s")[0].text, "legacy")

    def test_active_aborted_orphan_mismatch_shell(self):
        rows = [c_event("task_complete", last_agent_message="orphan"), c_event("task_started"), c_msg("active"),
                c_event("turn_aborted"),
                c_event("task_started"), c_event("task_complete")]
        self.assertFalse(any(t.selectable for t in codex.parse(rows, "s")))
        with self.assertRaises(TranscriptError):
            codex.parse([c_event("task_started", turn_id="a"), c_msg("wrong"),
                         c_event("task_complete", turn_id="b", last_agent_message="wrong")], "s")
        self.assertFalse(codex.parse([c_event("task_started"), c_msg("commentary"), c_event("task_complete")], "s")[0].selectable)
        self.assertFalse(codex.parse([c_event("task_started"), c_msg("draft", "final_answer"),
                                      c_event("task_complete", last_agent_message="draft", error={"message": "synthetic failure"})], "s")[0].selectable)

    def test_steering_tool_and_helper(self):
        rows = [c_event("task_started"), c_msg("question", role="user"), c_msg("before"),
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
                c_msg("steering", role="user"), c_msg("after", "final_answer"),
                c_event("task_complete", last_agent_message="after"),
                c_event("task_started"), c_msg("/copy-responses -1", role="user"), c_msg("helper", "final_answer"),
                c_event("task_complete", last_agent_message="helper")]
        turns = codex.parse(rows, "s")
        self.assertEqual([t.text for t in turns if t.selectable], ["before\n\nafter"])

    def test_unknown_and_duplicate(self):
        with self.assertRaises(TranscriptError):
            codex.parse([c_event("task_started"), c_msg("x", "mystery")], "s")
        with self.assertRaises(TranscriptError):
            codex.parse([c_event("task_started"), c_msg("one", "final"), c_msg("two", "final_answer")], "s")
        with self.assertRaises(TranscriptError):
            codex.parse([c_event("session_rollback")], "s")

    def test_versioned_rewrites_and_unknown_events(self):
        for event in ("thread_rolled_back", "context_compacted", "new_lifecycle_marker"):
            with self.subTest(event=event), self.assertRaises(TranscriptError):
                codex.parse([c_event("task_started"), c_msg("answer", "final_answer"),
                             c_event(event), c_event("task_complete", last_agent_message="answer")], "s")
        rows = [c_event("task_started"), c_event("item_completed", item={"type": "DynamicToolCall"}), c_event("token_count"),
                c_msg("answer", "final_answer"), c_event("task_complete", last_agent_message="answer")]
        self.assertEqual([t.text for t in codex.parse(rows, "s") if t.selectable], ["answer"])

    def test_item_completed_agent_message_must_be_mirrored(self):
        event = c_event("item_completed", item={"type": "AgentMessage", "id": "m1", "phase": "commentary",
                                                 "content": [{"type": "Text", "text": "progress"}]})
        rows = [c_event("task_started"), event, c_msg("done", "final_answer", id="m2"),
                c_event("task_complete", last_agent_message="done")]
        with self.assertRaises(TranscriptError):
            codex.parse(rows, "s")
        rows.insert(2, c_msg("progress", "commentary", id="m1"))
        self.assertEqual([t.text for t in codex.parse(rows, "s") if t.selectable], ["progress\n\ndone"])
        rows[2] = c_msg("changed", "commentary", id="m1")
        with self.assertRaises(TranscriptError):
            codex.parse(rows, "s")


class ClaudeTests(unittest.TestCase):
    def test_completed_and_tool_continuation(self):
        rows = [a_user("question"), a_msg([text("before"), {"type": "thinking", "thinking": "hidden"},
                {"type": "tool_use", "id": "tool1"}], "tool_use"),
                a_user([{"type": "tool_result", "tool_use_id": "tool1", "content": "secret"}], uuid="tool-result", parentUuid="synthetic-assistant"),
                a_msg([text("after\n```py\nx = 1\n```\n\t✓")], "end_turn", uuid="assistant-after", parentUuid="tool-result")]
        self.assertEqual([t.text for t in claude.parse(rows, "s") if t.selectable],
                         ["before\n\nafter\n```py\nx = 1\n```\n\t✓"])

    def test_eof_and_unknown_fail_closed(self):
        rows = [a_user("q"), a_msg([text("unfinished")], None)]
        self.assertFalse(any(t.selectable for t in claude.parse(rows, "s")))
        rows = [a_user("q"), a_msg([text("working"), {"type": "tool_use", "id": "tool1"}], "tool_use")]
        self.assertFalse(any(t.selectable for t in claude.parse(rows, "s")))
        rows.append(a_msg([text("premature final")], "end_turn", uuid="assistant-after", parentUuid="synthetic-assistant"))
        self.assertFalse(any(t.selectable for t in claude.parse(rows, "s")))
        rows = [a_user("q"), a_msg([text("unknown")], "mystery"),
                a_user("next", uuid="next-user", parentUuid="synthetic-assistant")]
        self.assertFalse(any(t.selectable for t in claude.parse(rows, "s")))

    def test_helper_meta_sidechain(self):
        rows = [a_user("<command-name>/copy-responses</command-name>", uuid="helper-user"),
                a_msg([text("helper")], "end_turn", uuid="helper-assistant", parentUuid="helper-user"),
                a_user("meta", isMeta=True, uuid="meta-user", parentUuid="helper-assistant"),
                a_msg([text("side")], "end_turn", isSidechain=True, uuid="side-assistant", parentUuid="meta-user"),
                a_user("human", uuid="real-user", parentUuid="helper-assistant"),
                a_msg([text("real")], "end_turn", uuid="real-assistant", parentUuid="real-user")]
        self.assertEqual([t.text for t in claude.parse(rows, "s") if t.selectable], ["real"])

    def test_synthetic_notices_are_not_responses(self):
        # Claude Code 2.1.280 --resume of a session with no model reply writes this pair first.
        synthetic = {"model": "<synthetic>", "id": "x"}
        rows = [a_user([text("Continue from where you left off.")], isMeta=True, uuid="resume-user"),
                a_msg([text("No response requested.")], "stop_sequence", uuid="resume-assistant", parentUuid="resume-user"),
                a_user("human", uuid="real-user", parentUuid="resume-assistant"),
                a_msg([text("API Error")], "stop_sequence", uuid="error-assistant", parentUuid="real-user", isApiErrorMessage=True),
                a_msg([text("real")], "end_turn", uuid="real-assistant", parentUuid="error-assistant")]
        for row in rows[1], rows[3]:
            row["message"].update(synthetic)
        self.assertEqual([t.text for t in claude.parse(rows, "s") if t.selectable], ["real"])
        with self.assertRaises(ValueError):  # an unattributed real model reply still fails closed
            claude.parse([rows[0], a_msg([text("x")], "end_turn", uuid="orphan", parentUuid="resume-user")], "s")

    def test_parent_chain_scope(self):
        direct = [a_user("q"), a_msg([text("ok")], "end_turn")]
        self.assertTrue(claude.parse(direct, "s")[0].selectable)
        attachment = {"type": "attachment", "uuid": "attachment-1", "parentUuid": "synthetic-user", "sessionId": "synthetic-session"}
        through_attachment = [direct[0], attachment, a_msg([text("ok")], "end_turn", parentUuid="attachment-1")]
        self.assertTrue(claude.parse(through_attachment, "s")[0].selectable)
        with self.assertRaises(TranscriptError):
            claude.parse([direct[0], {**attachment, "sessionId": "different-session"},
                          a_msg([text("cross-session")], "end_turn", parentUuid="attachment-1")], "s")
        for parent in ("missing-parent", "synthetic-assistant"):
            with self.subTest(parent=parent), self.assertRaises(TranscriptError):
                claude.parse([direct[0], a_msg([text("orphan")], "end_turn", parentUuid=parent)], "s")
        with self.assertRaises(TranscriptError):
            claude.parse([a_user("first", uuid="first-user"), a_user("second", uuid="second-user"),
                          a_msg([text("wrong branch")], "end_turn", parentUuid="first-user")], "s")

    def test_unknown_schema_identity(self):
        with self.assertRaises(TranscriptError):
            claude.parse([a_user("q"), a_msg([{"type": "new_visible_type"}], "end_turn")], "s")
        with self.assertRaises(TranscriptError):
            claude.parse([a_user("q"), a_msg([text("ok")], "end_turn", sessionId="other")], "s")


class JsonlTests(unittest.TestCase):
    def test_tail_and_interior(self):
        path = TMP / "partial.jsonl"
        first = json.dumps(c_event("task_started")) + "\n"
        path.write_text(first + '{"unfinished":', encoding="utf-8")
        self.assertEqual(len(records(path)), 1)
        path.write_text(first + json.dumps(c_event("task_complete")), encoding="utf-8")
        self.assertEqual(len(records(path)), 2)  # Valid final JSON needs no newline.
        path.write_text(first + "{bad}\n", encoding="utf-8")
        with self.assertRaises(TranscriptError):
            records(path)
        path.write_bytes(first.encode() + b'{"unfinished":"\xe2')
        self.assertEqual(len(records(path)), 1)
        path.write_bytes(first.encode() + b'{"bad":"\xff"}')
        with self.assertRaises(TranscriptError):
            records(path)
        path.write_text("{bad}\n" + first, encoding="utf-8")
        with self.assertRaises(TranscriptError):
            records(path)


class ClipboardTests(unittest.TestCase):
    def test_mock_only_and_states(self):
        with patch("response_history.clipboard.subprocess.run", side_effect=AssertionError("real clipboard runner")):
            runner = Mock(side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, stdout="é".encode())])
            self.assertEqual(copy_verified("é", runner).status, "verified")
            self.assertEqual(runner.call_args_list[0].args[0], ["/usr/bin/pbcopy"])
            self.assertEqual(runner.call_args_list[0].kwargs["input"], "é".encode())
            self.assertEqual(copy_verified("x", Mock(return_value=subprocess.CompletedProcess([], 1))).status, "write_failed")
            self.assertEqual(copy_verified("x", Mock(side_effect=[subprocess.CompletedProcess([], 0), OSError()])).status, "verification_unavailable")
            self.assertEqual(copy_verified("x", Mock(side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, stdout=b"y")])).status, "mismatch")


class CliTests(unittest.TestCase):
    def invoke(self, args, transport):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("response_history.clipboard.subprocess.run", side_effect=AssertionError("real clipboard")):
            status = main(args, transport=transport)
        return status, out.getvalue(), err.getvalue()

    def test_both_providers_and_no_leak(self):
        secret = "PRIVATE_SYNTHETIC_✓\n\n```\ncode\n```"
        cp = fixture("codex-cli.jsonl", [c_event("task_started"), c_msg(secret, "final_answer"), c_event("task_complete", last_agent_message=secret)])
        ap = fixture("claude-cli.jsonl", [a_user("q"), a_msg([text(secret)], "end_turn")])
        for provider, path in (("codex", cp), ("claude", ap)):
            with self.subTest(provider=provider):
                transport = Mock(return_value=CopyResult("verified"))
                base = ["--provider", provider, "--file", str(path)]
                status, out, err = self.invoke(base + ["copy"], transport)
                self.assertEqual(status, 0); self.assertNotIn(secret, out + err)
                transport.assert_called_once_with(secret)
                status, out, err = self.invoke(base + ["copy", "2"], transport)
                self.assertNotEqual(status, 0); self.assertNotIn(secret, out + err)
                self.assertEqual(transport.call_count, 1)
                status, out, err = self.invoke(base + ["copy", "--stdout"], transport)
                self.assertEqual(status, 0); self.assertEqual(out, secret)
                status, out, err = self.invoke(base + ["count"], transport)
                self.assertEqual((status, out.strip()), (0, "1"))
                status, out, err = self.invoke(base + ["list"], transport)
                self.assertEqual(status, 0); self.assertNotIn("\x1b", out)

    def test_auto_quiet_and_control(self):
        path = fixture("control.jsonl", [a_user("q"), a_msg([text("\x1b[2Jpayload")], "end_turn")])
        transport = Mock(return_value=CopyResult("verified"))
        base = ["--provider", "auto", "--file", str(path)]
        self.assertEqual(self.invoke(base + ["--quiet", "copy"], transport)[:2], (0, ""))
        self.assertNotIn("\x1b", self.invoke(base + ["list"], transport)[1])
        self.assertEqual(preview("\x1b[2Jpayload"), " [2Jpayload".strip())

    def test_source_and_transport_errors(self):
        path = fixture("source-error.jsonl", [a_user("q"), a_msg([text("PRIVATE_SYNTHETIC")], "end_turn")])
        transport = Mock(return_value=CopyResult("mismatch"))
        base = ["--provider", "claude", "--file", str(path)]
        status, out, err = self.invoke(base + ["copy"], transport)
        self.assertEqual(status, 3); self.assertNotIn("PRIVATE_SYNTHETIC", out + err)
        status, out, err = self.invoke(["--provider", "codex", "--file", str(path), "copy"], transport)
        self.assertEqual(status, 2); self.assertNotIn("PRIVATE_SYNTHETIC", out + err)
        missing = TMP / "missing-source.jsonl"
        self.assertEqual(self.invoke(["--provider", "auto", "--file", str(missing), "count"], transport)[0], 2)
        empty = TMP / "empty-source.jsonl"; empty.write_text("")
        self.assertEqual(self.invoke(["--provider", "auto", "--file", str(empty), "count"], transport)[0], 2)


if __name__ == "__main__":
    unittest.main()
