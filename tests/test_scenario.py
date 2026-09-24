import re
import unittest
from pathlib import Path

from tests import scenario

ROOT = Path(__file__).resolve().parents[1]


class ScenarioTests(unittest.TestCase):
    """The ./test demo scenario: every ls/copy form, both clients, fake clipboard."""

    @classmethod
    def setUpClass(cls):
        cls.claude, cls.codex = scenario.run("claude"), scenario.run("codex")
        cls.claude_stored, cls.codex_stored = scenario.run_stored("claude"), scenario.run_stored("codex")

    def test_every_command_is_answered_locally(self):
        for r in self.claude + self.codex:
            self.assertTrue(r["blocked"], r["command"])

    def test_copies_exactly_the_selected_replies(self):
        for r in self.claude:
            with self.subTest(command=r["command"]):
                if r["expected"] is None:
                    self.assertIsNone(r["copied"])  # lists and mistakes never touch the clipboard
                else:
                    self.assertEqual(r["copied"], "\n\n".join(scenario.REPLIES[i - 1] for i in r["expected"]))
                    self.assertEqual(r["reply"], "Copied " + ", ".join(f"#{i}" for i in r["expected"]))

    def test_list_forms(self):
        replies = {r["args"]: r["reply"] for r in self.claude if r["command"].startswith("/history-list")}
        self.assertEqual(replies["-3"], replies["3"])  # -N lists the latest N, like copy -N
        self.assertEqual(replies["3"].splitlines()[2:], [line for line in replies[""].splitlines()[2:]][-3:])
        self.assertEqual(replies["50"], replies[""])  # more than exist: all of them (6 <= the default 10)
        self.assertEqual(replies["0"], "Nothing listed: 0 isn't a valid count. "
                                       "Give how many of the latest responses to show, like 5 or -5.")
        self.assertTrue(replies["abc"].startswith("Nothing listed: can't read 'abc'."), replies["abc"])

    def test_terminal_layout_is_plain_and_complete(self):
        text = scenario.render_terminal(self.claude)
        self.assertNotIn("```", text)
        self.assertNotIn("## ", text)
        for r in self.claude:
            self.assertIn(f"> {r['command']}\n", text)
        self.assertEqual(text.count("Clipboard: unchanged"), sum(r["copied"] is None for r in self.claude))

    def test_mistakes_say_what_was_wrong(self):
        for r in self.claude + self.codex:
            if r["expected"] is None and r["command"].split()[0].endswith("history-copy"):
                with self.subTest(command=r["command"]):
                    self.assertTrue(r["reply"].startswith("Nothing copied:"), r["reply"])
                    for internal in ("Error", "ValueError", "Traceback"):
                        self.assertNotIn(internal, r["reply"])

    def test_claude_and_codex_give_identical_results(self):
        for a, b in zip(self.claude, self.codex):
            self.assertEqual((a["args"], a["reply"], a["copied"]), (b["args"], b["reply"], b["copied"]))

    def test_examples_doc_matches_the_tool(self):
        self.assertEqual((ROOT / "docs/examples.md").read_text(encoding="utf-8"), scenario.render(self.claude, self.claude_stored),
                         "docs/examples.md is out of date: run ./test --examples")


    def test_retired_names_are_not_answered(self):
        # The v2.1 names were retired by the installer migration: the hook leaves them to the client.
        for provider in ("claude", "codex"):
            old = scenario.run(provider, ("ls-responses", "copy-responses"))
            self.assertTrue(old and all(not r["blocked"] and r["reply"] is None and r["copied"] is None for r in old))

    def test_history_use_flow(self):
        cmds = [r for r in self.claude_stored if "command" in r]
        self.assertTrue(all(r["blocked"] for r in cmds))
        self.assertTrue(all(r["copied"] is None for r in cmds if not r["command"].startswith("/history-copy")))
        (stored, list0, use, ls, copy2, copy9, list1, live, back, live_again, reuse, removed) = cmds
        self.assertTrue(stored["reply"].startswith('Stored login-fix (3 replies), named after "Login fix".'))
        self.assertIn(f"{scenario.DEMO_HOME}/.agent-response-history/login-fix", stored["reply"])
        self.assertEqual(list0["reply"].splitlines()[0], "Current source: LIVE")
        self.assertIn("login-fix            here   Claude   3         15 Jan 2026  Safari reload bug", list0["reply"])
        self.assertTrue(use["reply"].startswith("Current source: stored session login-fix (3 replies, Claude Code)."))
        self.assertIn(f"cd '{scenario.DEMO_HOME}' && claude --resume '{scenario.EARLIER_SESSION}'", use["reply"])  # A: shown, never run
        self.assertEqual(ls["reply"].splitlines()[0], "Stored session: login-fix")  # never looks like live mode
        self.assertEqual(len(ls["reply"].splitlines()), 2 + 1 + len(scenario.EARLIER))
        self.assertEqual(copy2["reply"], "Copied #2, #3 from stored session login-fix")
        self.assertEqual(copy2["copied"], "\n\n".join(r for _, r in scenario.EARLIER[1:3]))
        self.assertEqual((copy9["copied"], copy9["reply"]),
                         (None, "Nothing copied: there is no response #9; this stored session has #1 to #3."))
        self.assertEqual(list1["reply"].splitlines()[0], "Current source: stored session login-fix")
        self.assertEqual(live["reply"].splitlines()[0], "Current source: LIVE.")
        self.assertNotIn("Stored session", back["reply"])  # back to live: the current fake session's #5, #6
        self.assertEqual([line.split()[0] for line in back["reply"].splitlines()[2:]], ["#5", "#6"])
        self.assertEqual(live_again["reply"], "Current source: LIVE (unchanged).")
        self.assertEqual(reuse["reply"], use["reply"])
        self.assertIsNone(removed["copied"])  # a removed archive is refused, never replaced by live
        self.assertTrue(removed["reply"].startswith("The selected stored session is no longer available ("), removed["reply"])

    def test_stored_mode_is_identical_in_claude_and_codex(self):
        def norm(s):  # only the client's own name, resume command and command prefix may differ
            s = re.sub(r"claude --resume|codex resume", "RESUME", s or "")
            s = re.sub(r"Claude Code|Claude|Codex", "CLIENT", s)
            return re.sub(r" +", " ", re.sub(r"(?<![\w<])[/$](history-)", r"/\1", s))
        strip = lambda rs: [(r.get("step"), r.get("args"), norm(r.get("reply")), r.get("copied")) for r in rs]
        self.assertEqual(strip(self.claude_stored), strip(self.codex_stored))

class ReadmeTests(unittest.TestCase):
    """The README keeps syntax only; exact output lives in the generated docs/examples.md."""

    def test_readme_commands_are_current_and_output_is_not_copied(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("(docs/examples.md)", text)
        self.assertIn("./test --examples", text)
        blocks = text.split("```")[1::2]
        commands = [line.split()[0] for block in blocks for line in block.splitlines()[1:]
                    if line.startswith(("/", "$")) or line.startswith("history-")]
        self.assertTrue(commands)
        for command in commands:
            self.assertRegex(command, r"^[/$]?history-(list|copy|store|use)$")
        for output in ("No.  Preview", "Copied #", "Current source: stored"):  # generated output belongs in docs/examples.md
            self.assertFalse(any(output in block for block in blocks), output)


if __name__ == "__main__":
    unittest.main()
