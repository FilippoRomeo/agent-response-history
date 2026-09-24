import json
import os
import stat
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from integrations.hook import handle
from response_history import history_store
from response_history.adapters import codex, claude

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
LEAKS = ("HIDDEN_THINKING", "TOOL_CALL_INPUT", "TOOL_RESULT_SECRET", "SYNTHETIC_NOTICE", "META_PROMPT",
         "SIDECHAIN_TEXT", "UNFINISHED_DRAFT", "HELPER_OUTPUT", "copy-responses", "AGENTS_MD_BODY",
         "ENV_CONTEXT", "IDE_TAB_LIST", "REASONING_TEXT", "DEVELOPER_TEXT", "ABORTED_DRAFT", "FUNCTION_OUTPUT",
         "FALLBACK_NOTICE", "store-history", "retrieve-history")


def claude_rows(session):
    def user(uid, parent, content, **extra):
        return {"type": "user", "uuid": uid, "parentUuid": parent, "isSidechain": False, "sessionId": session,
                "message": {"role": "user", "content": content}, **extra}

    def assistant(uid, parent, mid, content, stop, **extra):
        message = {"id": mid, "role": "assistant", "stop_reason": stop, "content": content}
        message.update(extra.pop("message", {}))
        return {"type": "assistant", "uuid": uid, "parentUuid": parent, "isSidechain": False, "sessionId": session,
                "message": message, **extra}

    text = lambda s: {"type": "text", "text": s}  # noqa: E731
    return [
        user("u1", None, "Fix the rocket ✓\n\n```py\nx = 1\n```"),
        assistant("a1", "u1", "m1", [text("Looking."), {"type": "thinking", "thinking": "HIDDEN_THINKING"},
                                     {"type": "tool_use", "id": "t1", "input": {"cmd": "TOOL_CALL_INPUT"}}], "tool_use"),
        user("r1", "a1", [{"type": "tool_result", "tool_use_id": "t1", "content": "TOOL_RESULT_SECRET"}]),
        assistant("a2", "r1", "m2", [text("Fixed it.")], "end_turn"),
        user("meta", "a2", "META_PROMPT", isMeta=True),
        assistant("syn", "meta", "m3", [text("SYNTHETIC_NOTICE")], "stop_sequence", message={"model": "<synthetic>"}),
        {"type": "assistant", "uuid": "side", "parentUuid": "meta", "isSidechain": True, "sessionId": session,
         "message": {"content": [text("SIDECHAIN_TEXT")], "stop_reason": "end_turn"}},
        user("h1", "a2", "<command-name>/copy-responses</command-name>"),
        assistant("h1a", "h1", "m4", [text("HELPER_OUTPUT")], "end_turn"),
        user("h2", "h1a", "<command-message>store-history</command-message>\n<command-name>/store-history</command-name>\n<command-args>x</command-args>"),
        assistant("h2a", "h2", "m7", [text("FALLBACK_NOTICE")], "end_turn"),
        user("u2", "h2a", [text("<ide_opened_file>IDE_TAB_LIST</ide_opened_file>"), text("Now the camera")]),
        assistant("a3", "u2", "m5", [text("Camera done.")], "end_turn"),
        user("u3", "a3", "and the fuel?"),
        assistant("a4", "u3", "m6", [text("UNFINISHED_DRAFT")], None),
    ]


def codex_rows(session):
    def event(kind, **values):
        return {"type": "event_msg", "payload": {"type": kind, **values}}

    def msg(role, text, phase=None, **values):
        kind = "input_text" if role in ("user", "developer") else "output_text"
        texts = text if isinstance(text, list) else [text]
        return {"type": "response_item", "payload": {"type": "message", "role": role, "phase": phase,
                                                     "content": [{"type": kind, "text": t} for t in texts], **values}}

    ide = "# Context from my IDE setup:\n\n## Open tabs:\n- IDE_TAB_LIST\n\n## My request for Codex:\nshader: why black?"
    return [
        {"type": "session_meta", "payload": {"id": session}},
        event("task_started", turn_id="t1"),
        msg("developer", "DEVELOPER_TEXT"),
        msg("user", ["# AGENTS.md instructions\n\n<INSTRUCTIONS>AGENTS_MD_BODY</INSTRUCTIONS>",
                     "<environment_context>\n  <cwd>ENV_CONTEXT</cwd>\n</environment_context>",
                     "Debug the texture pipeline"]),
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "REASONING_TEXT"}]}},
        msg("assistant", "Checking.", "commentary"),
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "TOOL_CALL_INPUT"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "FUNCTION_OUTPUT"}},
        msg("assistant", "Pipeline fixed.", "final_answer"),
        event("task_complete", turn_id="t1", last_agent_message="Pipeline fixed."),
        event("task_started", turn_id="t2"),
        msg("user", ide),
        msg("assistant", "Gamma was wrong.", "final_answer"),
        event("task_complete", turn_id="t2", last_agent_message="Gamma was wrong."),
        event("task_started", turn_id="t3"),
        msg("user", "$copy-responses"),
        msg("assistant", "HELPER_OUTPUT", "final_answer"),
        event("task_complete", turn_id="t3", last_agent_message="HELPER_OUTPUT"),
        event("task_started", turn_id="t3b"),
        msg("user", "$retrieve-history list"),
        msg("assistant", "FALLBACK_NOTICE", "final_answer"),
        event("task_complete", turn_id="t3b", last_agent_message="FALLBACK_NOTICE"),
        event("task_started", turn_id="t4"),
        msg("user", "try again"),
        msg("assistant", "ABORTED_DRAFT", "final_answer"),
        event("turn_aborted"),
    ]


CLAUDE_MD = """# Session: rocket

## User

Fix the rocket ✓

```py
x = 1
```

## Assistant

Looking.

Fixed it.

## User

Now the camera

## Assistant

Camera done.
"""

CODEX_MD = """# Session: shader

## User

Debug the texture pipeline

## Assistant

Checking.

Pipeline fixed.

## User

shader: why black?

## Assistant

Gamma was wrong.
"""


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        patches = [patch.dict(os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                                           "CODEX_HOME": str(self.home / ".codex")}),
                   patch("subprocess.run", side_effect=AssertionError("clipboard or subprocess used")),
                   patch("response_history.clipboard.copy_verified", side_effect=AssertionError("clipboard used"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.base / "work" / "ka"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "src" / "deep").mkdir(parents=True)
        self.claude_sid, self.codex_sid = str(uuid.uuid4()), str(uuid.uuid4())
        self.claude_path = write_rows(self.home / ".claude/projects/-work-ka" / f"{self.claude_sid}.jsonl", claude_rows(self.claude_sid))
        self.codex_path = write_rows(self.home / ".codex/sessions/2026/09/23" / f"rollout-x-{self.codex_sid}.jsonl", codex_rows(self.codex_sid))

    def store(self, provider="claude", name="rocket", note="", cwd=None, now=NOW):
        sid, path = (self.claude_sid, self.claude_path) if provider == "claude" else (self.codex_sid, self.codex_path)
        return history_store.store(provider, sid, path, str(cwd or self.repo), name, note, now)

    def project(self):
        return history_store.project_dir(self.repo)


class ParserPromptTests(Env):
    def test_claude_prompt_field(self):
        turns = [t for t in claude.parse(claude_rows(self.claude_sid), "s") if t.selectable]
        self.assertEqual([t.prompt for t in turns], ["Fix the rocket ✓\n\n```py\nx = 1\n```", "Now the camera"])
        self.assertEqual([t.text for t in turns], ["Looking.\n\nFixed it.", "Camera done."])

    def test_codex_prompt_field(self):
        turns = [t for t in codex.parse(codex_rows(self.codex_sid), "s") if t.selectable]
        self.assertEqual([t.prompt for t in turns], ["Debug the texture pipeline", "shader: why black?"])
        self.assertEqual([t.text for t in turns], ["Checking.\n\nPipeline fixed.", "Gamma was wrong."])

    def test_codex_steering_joins_turn_prompt(self):
        rows = [{"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "steer"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "a"}]}},
                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "a"}}]
        self.assertEqual(codex.parse(rows, "s")[0].prompt, "question\n\nsteer")

    def test_history_commands_are_excluded_helpers(self):
        self.assertEqual(claude.parse([{"type": "user", "uuid": "u", "message": {"content": "<command-name>/store-history</command-name>"}}], "s")[0].excluded, "helper command")
        for prompt in ("$store-history x", "$retrieve-history", "/retrieve-history"):
            rows = [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}}]
            self.assertEqual(codex.parse(rows, "s")[0].excluded, "helper command", prompt)


class StoreTests(Env):
    def test_claude_exact_files(self):
        path, count, durable = self.store(note="Fixed rocket")
        self.assertEqual((path, count, durable), (self.project() / "rocket", 2, True))
        self.assertEqual(sorted(p.name for p in path.iterdir()), ["meta.json", "replies.md", "turns.jsonl"])
        self.assertEqual((path / "replies.md").read_text(encoding="utf-8"), CLAUDE_MD)
        meta = json.loads((path / "meta.json").read_bytes().decode("utf-8"))
        self.assertEqual(meta, {
            "schema_version": 2, "name": "rocket", "note": "Fixed rocket", "provider": "claude",
            "session_id": self.claude_sid, "project_path": str(self.repo), "cwd": str(self.repo),
            "stored_at": NOW.astimezone().isoformat(timespec="seconds"), "reply_count": 2,
            "first_prompt_preview": "Fix the rocket ✓ ```py x = 1 ```", "last_reply_preview": "Camera done."})
        self.assertIsNotNone(datetime.fromisoformat(meta["stored_at"]).tzinfo)

    def test_codex_exact_files(self):
        path, _, _ = self.store("codex", "shader")
        self.assertEqual((path / "replies.md").read_text(encoding="utf-8"), CODEX_MD)
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["provider"], meta["session_id"], meta["note"], meta["reply_count"]), ("codex", self.codex_sid, "", 2))
        self.assertEqual((meta["first_prompt_preview"], meta["last_reply_preview"]), ("Debug the texture pipeline", "Gamma was wrong."))

    def test_privacy_no_leaks(self):
        for provider, name in (("claude", "rocket"), ("codex", "shader")):
            path, _, _ = self.store(provider, name)
            stored = (path / "replies.md").read_text() + (path / "meta.json").read_text()
            for leak in LEAKS:
                self.assertNotIn(leak, stored, (provider, leak))

    def test_project_partition(self):
        sub, _, _ = self.store(cwd=self.repo / "src" / "deep")
        self.assertEqual(sub.parent, self.project())
        self.assertTrue(sub.parent.name.startswith("ka--"))
        self.assertEqual(json.loads((sub / "meta.json").read_text())["cwd"], str(self.repo / "src" / "deep"))
        worktree = self.base / "wt" / "ka"
        (worktree / "a").mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: elsewhere\n")  # .git file, still a root
        other, _, _ = self.store(cwd=worktree / "a")
        self.assertEqual(other.parent.name.split("--")[0], "ka")
        self.assertNotEqual(other.parent, sub.parent)  # same basename, different path
        plain = self.base / "plain" / "dir"
        plain.mkdir(parents=True)
        self.assertEqual(history_store.project_root(str(plain)), plain)
        self.assertEqual(history_store.project_dir(plain), history_store.project_dir(Path(str(plain))))
        linked = self.base / "link"
        linked.symlink_to(self.repo)
        self.assertEqual(history_store.project_root(str(linked / "src")), self.repo)

    def test_names(self):
        for bad in ("", "a b", "a/b", "../x", ".hidden", "a.b", "é", "x" * 65, "a\n", "-" * 0):
            with self.subTest(bad=bad), self.assertRaises(history_store.HistoryError):
                self.store(name=bad)
        self.assertFalse(history_store.store_root().exists())  # rejected before any filesystem mutation
        self.store(name="x" * 64)
        self.store(name="A-z_9")

    def test_duplicate_refused_and_original_kept(self):
        path, _, _ = self.store(note="first")
        before = {p.name: p.read_bytes() for p in path.iterdir()}
        with self.assertRaises(history_store.HistoryError):
            self.store("codex", note="second")
        self.assertEqual({p.name: p.read_bytes() for p in path.iterdir()}, before)
        self.assertEqual([p.name for p in self.project().iterdir()], ["rocket"])

    def assert_refused_leaving(self, target, before):
        self.assertEqual((target.stat().st_ino, target.stat().st_mtime_ns), before)  # same directory, untouched
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual([p.name for p in self.project().iterdir()], ["rocket"])  # no temp dirs left

    def test_existing_empty_destination_is_never_replaced(self):
        target = self.project() / "rocket"
        target.mkdir(parents=True)
        before = (target.stat().st_ino, target.stat().st_mtime_ns)
        with self.assertRaises(history_store.HistoryError):
            self.store()
        self.assert_refused_leaving(target, before)

    def test_destination_created_before_publication_is_never_replaced(self):
        target = self.project() / "rocket"
        real, before = history_store._publish, []

        def racing(src, dst):  # another writer claims the name after every pre-check
            target.mkdir()
            before.append((target.stat().st_ino, target.stat().st_mtime_ns))
            real(src, dst)

        with patch.object(history_store, "_publish", racing), self.assertRaises(history_store.HistoryError):
            self.store()
        self.assert_refused_leaving(target, before[0])

    def test_permissions(self):
        path, _, _ = self.store()
        for folder in (history_store.store_root(), self.project(), path):
            self.assertEqual(mode(folder), 0o700, folder)
        for file in path.iterdir():
            self.assertEqual(mode(file), 0o600, file)

    def test_failed_write_leaves_nothing(self):
        real = history_store._write
        calls = []

        def failing(path, text):
            calls.append(path.name)
            if path.name == "replies.md":
                raise OSError("disk full")
            real(path, text)

        with patch.object(history_store, "_write", failing), self.assertRaises(OSError):
            self.store()
        self.assertEqual(calls, ["turns.jsonl", "meta.json", "replies.md"])
        self.assertEqual(list(self.project().iterdir()), [])  # no final dir, no temp dir
        self.store()  # the name is still free

    def test_no_responses_refused(self):
        empty = write_rows(self.base / "empty.jsonl", [{"type": "session_meta", "payload": {"id": self.codex_sid}}])
        with self.assertRaises(history_store.HistoryError):
            history_store.store("codex", self.codex_sid, empty, str(self.repo), "x")
        with self.assertRaises(history_store.HistoryError):
            history_store.store("codex", self.codex_sid, self.base / "missing.jsonl", str(self.repo), "x")
        self.assertFalse((self.project() / "x").exists())


class RetrieveTests(Env):
    def test_listing_order_and_partition(self):
        listing = lambda cwd: history_store.use_listing("claude", str(cwd))  # noqa: E731
        self.assertTrue(listing(self.repo).startswith("No stored sessions"))
        self.store("codex", "shader", now=NOW - timedelta(days=1))
        self.store("claude", "rocket-nav", note="Fixed rocket navigation")
        self.store("claude", "b-same", now=NOW - timedelta(days=1))
        other = self.base / "elsewhere"
        other.mkdir()
        self.store(name="foreign", cwd=other)  # another project's v2.1 store
        day = lambda d: d.astimezone().strftime("%d %b %Y")  # noqa: E731
        self.assertEqual(listing(self.repo / "src").splitlines(), [
            "Name                 Where  Client   Replies   Stored       What it did",
            "-------------------  -----  -------  --------  -----------  ----------------------------",
            f"rocket-nav           v2.1   Claude   2         {day(NOW)}  Fixed rocket navigation",
            f"b-same               v2.1   Claude   2         {day(NOW - timedelta(days=1))}  Fix the rocket ✓ ```py x = 1 ```",
            f"shader               v2.1   Codex    2         {day(NOW - timedelta(days=1))}  Debug the texture pipeline",
        ])

    def test_resume_available_and_missing(self):
        self.store(note="Fixed rocket")
        self.store("codex", "shader")
        use = lambda name: history_store.use("claude", name, self.claude_sid, str(self.repo))  # noqa: E731
        self.assertIn(f"\ncd '{self.repo}' && claude --resume '{self.claude_sid}'", use("rocket"))
        self.assertIn(f"\ncd '{self.repo}' && codex resume '{self.codex_sid}'", use("shader"))
        self.codex_path.unlink()
        missing = use("shader")
        self.assertIn("isn't on this machine any more", missing)
        self.assertNotIn("codex resume", missing)
        self.assertTrue((self.project() / "shader" / "replies.md").is_file())
        self.assertTrue(use("nope").startswith("Nothing selected: no stored session named nope"))
        self.assertTrue(use("../rocket").startswith("Nothing selected: ../rocket is not a stored session folder"))

    def test_shell_quoting(self):
        odd = self.base / "it's a $dir"
        odd.mkdir()
        self.store(cwd=odd)
        reply = history_store.use("claude", "rocket", self.claude_sid, str(odd))
        self.assertIn(f"\ncd '{self.base}/it'\"'\"'s a $dir' && claude --resume '{self.claude_sid}'", reply)


class HistoryHookTests(Env):
    def claude(self, name, args, cwd=None):
        return handle({"hook_event_name": "UserPromptExpansion", "session_id": self.claude_sid,
                       "transcript_path": str(self.claude_path), "cwd": str(cwd or self.repo),
                       "command_name": name, "command_args": args})

    def codex(self, prompt, cwd=None):
        return handle({"hook_event_name": "UserPromptSubmit", "session_id": self.codex_sid,
                       "transcript_path": str(self.codex_path), "cwd": str(cwd or self.repo), "prompt": prompt})

    def here(self, name=""):
        return history_store.here_root(str(self.repo)) / name

    def test_claude_commands(self):
        result = self.claude("history-store", ' --name rocket --note "Fixed rocket" ')
        self.assertEqual(result["reason"].splitlines()[:2], ["Stored rocket (2 replies).", str(self.here("rocket"))])
        self.assertEqual(json.loads(self.here("rocket/meta.json").read_text())["note"], "Fixed rocket")
        self.assertIn("rocket               here   Claude", self.claude("history-use", "list")["reason"])
        self.assertIn("claude --resume", self.claude("history-use", "rocket")["reason"])
        self.assertIn("already exists", self.claude("history-store", "--name rocket")["reason"])

    def test_note_accepts_single_or_double_quotes(self):
        self.claude("history-store", "--name one --note 'a note with \"inner\" quotes'")
        self.codex('$history-store --name two --note "a note with \'inner\' quotes"')
        notes = {n: json.loads(self.here(f"{n}/meta.json").read_text())["note"] for n in ("one", "two")}
        self.assertEqual(notes, {"one": 'a note with "inner" quotes', "two": "a note with 'inner' quotes"})
        self.assertIn("Nothing stored.", self.claude("history-store", "--name three unquoted note")["reason"])

    def test_codex_commands(self):
        self.assertEqual(self.codex("$history-store --name shader")["reason"].splitlines()[:2],
                         ["Stored shader (2 replies).", str(self.here("shader"))])
        self.assertIn("shader               here   Codex", self.codex("$history-use list")["reason"])
        self.assertEqual(self.codex("$history-use  list "), self.codex("$history-use list"))
        self.assertEqual(self.claude("history-use", "list"), self.claude("history-use", ""))
        self.assertIn("Usage", self.codex("$history-use list shader")["reason"])
        self.assertIn("reserved", self.codex("$history-store --name list")["reason"])
        self.assertIn("reserved", self.claude("history-store", '--name LIVE --note "x"')["reason"])
        self.assertFalse(self.here("list").exists())
        self.assertIn("codex resume", self.codex("$history-use shader")["reason"])

    def test_malformed_rejected_locally(self):
        for prompt in ("$history-store --name bad/name", "$history-store a b", '$history-store --name "x', "$history-store --bogus",
                       "$history-store a\nb", "$history-use a b", "$history-store --name " + "x" * 65):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.codex(prompt)["decision"], "block")
        for args in ("a b", "--name bad/name", "a\nb", "--note"):
            self.assertEqual(self.claude("history-store", args)["decision"], "block")
        self.assertFalse(self.here().exists())
        self.assertFalse(history_store.store_root().exists())
        event = {"hook_event_name": "UserPromptSubmit", "session_id": self.codex_sid, "transcript_path": str(self.codex_path),
                 "prompt": "$history-store --name x"}
        self.assertEqual(handle(event)["reason"], "Response history session unavailable")  # no cwd
        self.assertEqual(handle({**event, "cwd": "relative"})["reason"], "Response history session unavailable")

    def test_prose_passes_through(self):
        for prompt in ("please $history-store x", "$history-storex", "$history-usex", "/history-store x", "ordinary",
                       "$history-use$history-use list", "run $history-use list", "$store-history x", "$retrieve-history list"):
            self.assertIsNone(self.codex(prompt), prompt)
        self.assertIsNone(self.claude("history-storex", "x"))
        self.assertIsNone(self.claude("store-history", "x"))  # retired: no longer answered by the hook
        self.assertIsNone(handle({"hook_event_name": "UserPromptSubmit", "prompt": "/history-store x", "session_id": self.claude_sid}))

    def test_fallback_command_files_fail_closed(self):
        for name, outcome in (("history-store", "nothing was stored"), ("history-use", "no stored session was selected")):
            body = (ROOT / f"integrations/claude/{name}.md").read_text().split("---", 2)[2]
            self.assertIn(f"The agent-response-history hook did not run in this client, so {outcome}", body)
            self.assertIn("Do not run commands, read files", body)


class PromptFilterTests(unittest.TestCase):
    # Layout of a real Codex VS Code prompt (Codex 0.156.0); every non-heading character masked.
    IDE = ("# Context from my IDE setup:\n\n## Active file: src/x.js\n\n## Active selection of the file:\n"
           "// xxx/xxxx/xxxxxxxxxx.xx\n\nxxxxx xxxxx = {\n  xxxx: \"/xxxxxx/xxxx.xxx\",\n\n"
           "## Open tabs:\n- x.js: src/x.js\n\n## My request for Codex:\nwhy is the <canvas> black?\n")

    def test_human_text_survives(self):
        from response_history.adapters.common import prompt_text
        self.assertEqual(prompt_text("Please fix the bug in main.py"), "Please fix the bug in main.py")
        inline = "Why does <div> collapse inside <span>text</span>? See <b>this</b>"
        self.assertEqual(prompt_text(inline), inline)
        self.assertEqual(prompt_text([{"type": "input_text", "text": inline}]), inline)
        self.assertEqual(prompt_text([{"type": "input_text", "text": self.IDE}]), "why is the <canvas> black?\n")
        for document in ('<svg viewBox="0 0 1 1"><path d="M0 0"/></svg>', "<config>\n  <a>1</a>\n</config>",
                         "<b>bold</b>", "<send_user_message_question_reply>yes, option 2</send_user_message_question_reply>",
                         "<user_shell_command>ls -la</user_shell_command>", "<bash-input>git status</bash-input>"):
            self.assertEqual(prompt_text(document), document)
            self.assertEqual(prompt_text([{"type": "input_text", "text": document}]), document)

    def test_known_injected_blocks_dropped(self):
        from response_history.adapters.common import prompt_text
        injected = ["# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>",
                    "<environment_context>\n  <cwd>/x</cwd>\n  <shell>zsh</shell>\n</environment_context>",
                    "<recommended_plugins>\nplugins\n</recommended_plugins>", "<skill>\n<name>x</name>\n</skill>",
                    "<turn_aborted>\nThe previous turn was interrupted\n</turn_aborted>",
                    "<ide_opened_file>The user opened /x.py</ide_opened_file>",
                    "<ide_selection>lines 1-2</ide_selection>", "<user_instructions>\nx\n</user_instructions>",
                    "<guardian_tool_descriptions>\nx\n</guardian_tool_descriptions>",
                    "<guardian_context_omission>x</guardian_context_omission>",
                    "<external_codex_apps_writing_block_edits>x</external_codex_apps_writing_block_edits>",
                    "<local-command-stdout>Copied</local-command-stdout>", "<local-command-stderr>x</local-command-stderr>",
                    "<task-notification>\n<task-id>x</task-id>\n</task-notification>"]
        content = [{"type": "input_text", "text": t} for t in injected] + [{"type": "input_text", "text": "real ask"}]
        self.assertEqual(prompt_text(content), "real ask")
        for block in injected:
            self.assertEqual(prompt_text(block), "", block)


class SharedLoaderTests(Env):
    def test_load_turns_is_exactly_the_adapter_output(self):
        from response_history import cli, transcript
        from response_history.adapters.common import records
        from response_history.model import TranscriptError
        for provider, sid, path, parse in (("claude", self.claude_sid, self.claude_path, claude.parse),
                                           ("codex", self.codex_sid, self.codex_path, codex.parse)):
            with self.subTest(provider=provider):
                expected = [t for t in parse(records(path), str(path)) if t.selectable]
                self.assertEqual(transcript.load_turns(provider, path, sid), expected)
                self.assertEqual(transcript.load_turns("auto", path, sid), expected)
                self.assertEqual(transcript.load_turns(provider, path), expected)
                with self.assertRaises(TranscriptError):
                    transcript.load_turns(provider, path, str(uuid.uuid4()))
        self.assertIs(cli.preview, transcript.preview)
        self.assertIs(history_store.preview, transcript.preview)

    def test_preview_contract_is_unchanged(self):
        from response_history.transcript import preview
        self.assertEqual(preview("x" * 200), "x" * 120)  # meta.json previews are capped at 120 characters
        self.assertEqual(preview("a\tb\n\n c\x1b[2Jd"), "a b c [2Jd")  # control characters become spaces, runs collapse


class DurabilityTests(Env):
    def fail_fsync(self, when):
        real = history_store._fsync_dir

        def fsync(path):
            if when(Path(path)):
                raise OSError("fsync failed")
            real(path)
        return patch.object(history_store, "_fsync_dir", fsync)

    def run_store(self, args):
        return history_store.run("claude", "history-store", args, self.claude_sid, str(self.claude_path), str(self.repo))

    def test_parent_fsync_failure_after_publish_reports_stored_with_warning(self):
        here = history_store.here_root(str(self.repo))
        with self.fail_fsync(lambda p: p in (self.project(), here)):
            path, count, durable = self.store()
            message = self.run_store("--name second")
        self.assertEqual((path, count, durable), (self.project() / "rocket", 2, False))
        self.assertEqual((path / "replies.md").read_text(encoding="utf-8"), CLAUDE_MD)
        self.assertEqual(json.loads((path / "meta.json").read_text(encoding="utf-8"))["reply_count"], 2)
        self.assertEqual(message.splitlines()[0], "Stored second (2 replies).")
        self.assertEqual(message.splitlines()[-1], "Warning: the session was stored, but filesystem durability could not be confirmed.")
        self.assertNotIn("error", message.lower())
        self.assertEqual(sorted(p.name for p in here.iterdir()), ["second"])  # no temp dir left
        with self.assertRaises(history_store.HistoryError):
            self.store()  # the name is taken: no duplicate
        self.assertIn("already exists", self.run_store("--name second"))

    def test_fsync_failure_before_publish_stores_nothing(self):
        here = history_store.here_root(str(self.repo))
        with self.fail_fsync(lambda p: p.name.startswith(".tmp-")):
            with self.assertRaises(OSError):
                self.store()
            message = self.run_store("--name rocket")
        self.assertEqual(message, "Stored-session error: OSError")
        self.assertEqual(list(self.project().iterdir()), [])  # no final dir, no temp dir
        self.assertEqual(list(here.iterdir()), [])
        self.assertEqual(self.store()[:2], (self.project() / "rocket", 2))  # the name is still free

    def test_durable_store_has_no_warning(self):
        message = self.run_store("--name rocket")
        self.assertEqual(message.splitlines()[:2], ["Stored rocket (2 replies).", str(history_store.here_root(str(self.repo)) / "rocket")])
        self.assertNotIn("Warning", message)

if __name__ == "__main__":
    unittest.main()
