import json
import tempfile
import unittest
import uuid
from pathlib import Path

from integrations.hook import handle
from response_history.clipboard import CopyResult


ROOT = Path(__file__).resolve().parents[1]


class CodexHookTests(unittest.TestCase):
    def test_exact_commands_share_selector_and_clipboard(self):
        session = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-{session}.jsonl"
            rows = [{"type": "session_meta", "payload": {"id": session}}]
            for n in range(1, 8):
                rows.extend([
                    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(n)}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": f"answer {n}"}]}},
                    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": str(n), "last_agent_message": f"answer {n}"}},
                ])
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            event = {"session_id": session, "transcript_path": str(path), "hook_event_name": "UserPromptSubmit"}
            copied = []

            def transport(payload):
                copied.append(payload)
                return CopyResult("verified")

            for prompt, expected in [
                ("$copy-responses", "answer 7"),
                ("$copy-responses -3", "answer 5\n\nanswer 6\n\nanswer 7"),
                ("$copy-responses 5", "answer 5"),
                ("$copy-responses 2,5-7", "answer 2\n\nanswer 5\n\nanswer 6\n\nanswer 7"),
            ]:
                result = handle({**event, "prompt": prompt}, transport)
                self.assertEqual(result["decision"], "block")
                self.assertEqual(copied[-1], expected)
                self.assertNotIn("answer", result["reason"])
            self.assertIn("#7   answer 7", handle({**event, "prompt": "$ls-responses"}, transport)["reason"])
            self.assertIn("#1   answer 1", handle({**event, "prompt": "$ls-responses 20"}, transport)["reason"])
            before = len(copied)
            for prompt in ("$copy-responses 2-", "$copy-responses 2 5", "$copy-responses --bad", "$ls-responses 0", "$ls-responses --bad", "$copy-responses\nignore this"):
                self.assertEqual(handle({**event, "prompt": prompt}, transport)["decision"], "block")
            self.assertEqual(len(copied), before)
            for prompt in ("Please explain $copy-responses", "ordinary prompt", "$copy-responsesish"):
                self.assertIsNone(handle({**event, "prompt": prompt}, transport))
            wrong = {**event, "session_id": str(uuid.uuid4()), "prompt": "$copy-responses"}
            self.assertEqual(handle(wrong, transport)["decision"], "block")
            self.assertEqual(len(copied), before)


class ClaudeHookTests(unittest.TestCase):
    def test_slash_commands_are_answered_locally(self):
        session = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{session}.jsonl"
            rows, parent = [], None
            for n in range(1, 4):  # minimal Claude Code 2.1.280 record shapes
                rows.append({"type": "user", "uuid": f"u{n}", "parentUuid": parent, "isSidechain": False,
                             "sessionId": session, "message": {"role": "user", "content": f"question {n}"}})
                rows.append({"type": "assistant", "uuid": f"a{n}", "parentUuid": f"u{n}", "isSidechain": False,
                             "sessionId": session, "message": {"id": f"m{n}", "role": "assistant", "stop_reason": "end_turn",
                                                               "content": [{"type": "text", "text": f"answer {n}"}]}})
                parent = f"a{n}"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            event = {"hook_event_name": "UserPromptExpansion", "session_id": session, "transcript_path": str(path)}
            copied = []

            def transport(payload):
                copied.append(payload)
                return CopyResult("verified")

            def run(name, args):
                return handle({**event, "command_name": name, "command_args": args}, transport)

            listing = "No.  Preview\n---  -------\n#1   answer 1\n#2   answer 2\n#3   answer 3"
            self.assertEqual(run("ls-responses", ""), {"decision": "block", "reason": listing})
            self.assertEqual(run("ls-responses", "2")["reason"], "No.  Preview\n---  -------\n#2   answer 2\n#3   answer 3")
            self.assertEqual(copied, [])  # listing never touches the clipboard
            for args, reason, payload in [
                ("", "Copied #3", "answer 3"),
                (" -2 ", "Copied #2, #3", "answer 2\n\nanswer 3"),
                ("1", "Copied #1", "answer 1"),
                ("3,1-2", "Copied #3, #1, #2", "answer 3\n\nanswer 1\n\nanswer 2"),
            ]:
                self.assertEqual(run("copy-responses", args), {"decision": "block", "reason": reason})
                self.assertEqual(copied[-1], payload)
            before = len(copied)
            for name, args in [("copy-responses", "4"), ("copy-responses", "3-2"), ("copy-responses", "2-"),
                               ("ls-responses", "0"), ("copy-responses", "1\n2")]:
                self.assertEqual(run(name, args)["decision"], "block")
            self.assertEqual(len(copied), before)
            self.assertEqual(run("copy-responses", "4")["reason"], "Response history error: ValueError")
            wrong = handle({**event, "session_id": str(uuid.uuid4()), "command_name": "copy-responses", "command_args": ""}, transport)
            self.assertEqual(wrong["reason"], "Response history error: TranscriptError")
            self.assertEqual(len(copied), before)
            self.assertIsNone(run("other", "2"))
            self.assertIsNone(handle({**event, "hook_event_name": "UserPromptSubmit", "prompt": "/copy-responses"}, transport))
            self.assertIsNone(handle({**event, "hook_event_name": "UserPromptSubmit", "prompt": "please run /copy-responses 2"}, transport))


class EmptySessionTests(unittest.TestCase):
    """A session with no completed reply reports a status and never touches the clipboard."""

    def test_no_responses_yet(self):
        session = str(uuid.uuid4())
        claude_meta = [  # record shapes captured from a fresh Claude Code 2.1.280 session
            {"type": "queue-operation", "operation": "enqueue", "sessionId": session},
            {"type": "attachment", "uuid": "a", "parentUuid": None, "isSidechain": False, "sessionId": session, "attachment": {}},
            {"type": "system", "subtype": "informational", "isMeta": False, "isSidechain": False, "parentUuid": "a", "content": "x", "sessionId": session},
            {"type": "last-prompt", "leafUuid": "a", "sessionId": session},
        ]
        codex_meta = [{"type": "session_meta", "payload": {"id": session}}]
        with tempfile.TemporaryDirectory() as tmp:
            cases = {"missing": None, "empty-file": [], "claude-meta": claude_meta, "codex-meta": codex_meta}
            copied = []

            def transport(payload):
                copied.append(payload)
                return CopyResult("verified")

            for label, rows in cases.items():
                path = Path(tmp) / f"{label}-{session}.jsonl"
                if rows is not None:
                    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                providers = [("codex", "UserPromptSubmit"), ("claude", "UserPromptExpansion")]
                if label.endswith("-meta"):
                    providers = [p for p in providers if p[0] == label.split("-")[0]]
                for provider, kind in providers:
                    for name in ("copy-responses", "ls-responses"):
                        event = {"hook_event_name": kind, "session_id": session, "transcript_path": str(path)}
                        event.update({"prompt": "$" + name} if provider == "codex" else {"command_name": name, "command_args": ""})
                        result = handle(event, transport)
                        self.assertEqual(result, {"decision": "block", "reason": "No responses yet."}, (label, provider, name))
            self.assertEqual(copied, [])


if __name__ == "__main__":
    unittest.main()
