import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from response_history.cli import main
from response_history.session import resolve


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_list_limit_keeps_absolute_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            rows = []
            for n in range(1, 4):
                answer = "answer | 2" if n == 2 else "x" * 100 if n == 3 else f"answer {n}"
                rows += [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(n)}},
                         {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": answer}]}},
                         {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": str(n), "last_agent_message": answer}}]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--provider", "codex", "--file", str(path), "list", "2"]), 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "No.  Preview", "---  -------",
                "#2   answer | 2", "#3   " + "x" * 67 + "…",
            ])

    def test_session_lookup_and_identity(self):
        sid = str(uuid.uuid4())
        # The managed sandbox permits fixture files but may deny directory removal.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_root = home / "codex-root"
            claude_root = home / "claude-root"
            cp = codex_root / "sessions" / "2026" / f"rollout-{sid}.jsonl"
            ap = claude_root / "projects" / "project" / f"{sid}.jsonl"
            cp.parent.mkdir(parents=True); ap.parent.mkdir(parents=True)
            cp.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
            ap.write_text(json.dumps({"type": "user", "sessionId": sid, "uuid": "u", "message": {"content": "q"}}) + "\n")
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_root), "CLAUDE_CONFIG_DIR": str(claude_root)}):
                self.assertEqual(resolve("codex", sid), cp)
                self.assertEqual(resolve("claude", sid), ap)
                for provider in ("codex", "claude"):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main(["--provider", provider, "--session", sid, "count"]), 0)
                ap.write_text(ap.read_text().replace(sid, str(uuid.uuid4())))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--provider", "claude", "--session", sid, "count"]), 2)
                duplicate = codex_root / "archived_sessions" / f"other-{sid}.jsonl"
                duplicate.parent.mkdir(parents=True); duplicate.write_text(cp.read_text())
                with self.assertRaises(ValueError):
                    resolve("codex", sid)

if __name__ == "__main__":
    unittest.main()
