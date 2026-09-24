"""One fake session, run through the real hook exactly as Claude Code and Codex would.

The same results feed three things: the unit tests assert them, ./test prints them as a
plain-terminal demo (render_terminal), and ./test --examples writes them as Markdown to
docs/examples.md (render).
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from integrations.hook import handle
from response_history.clipboard import CopyResult

SESSION = "00000000-0000-4000-8000-000000000001"
EXCHANGES = [
    ("Plan a one-page site for a bakery.", "Plan: a header with the name, a menu section, opening hours and a map."),
    ("Write the header.", "<header><h1>Crumb & Co.</h1></header>"),
    ("Suggest three menu items.", "Sourdough loaf, almond croissant, cardamom bun."),
    ("Style the menu as a grid.", ".menu {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);\n}"),
    ("Add the opening hours.", "Open Tuesday to Sunday, 7:00 to 15:00."),
    ("Summarise what we built.", "A header, a three-item menu grid and the opening hours."),
]
REPLIES = [reply for _, reply in EXCHANGES]

# An earlier session, stored as "login-fix", used as the source instead of the live one.
EARLIER_SESSION = "00000000-0000-4000-8000-000000000002"
EARLIER = [
    ("Why does the login form reload the page?", "The button is inside a <form> with no type, so it submits."),
    ("Fix it.", '<button type="button">Log in</button>'),
    ("Anything else?", "Also trim the email before sending it."),
]

DEMO_HOME = "/Users/you/login-app"  # stands in for the temporary folder the stored demo runs in

# (arguments, response numbers that end up on the clipboard; None = clipboard untouched)
LIST_CASES = ["", "3", "-3", "50", "0", "abc"]
COPY_CASES = [
    ("", [6]), ("-2", [5, 6]), ("-10", [1, 2, 3, 4, 5, 6]), ("4", [4]), ("1,3", [1, 3]),
    ("2-4", [2, 3, 4]), ("2, 6-6", [2, 6]), ("#5", [5]), ("3,1", [3, 1]),
    ("9", None), ("0", None), ("3-1", None), ("-11", None), ("2-", None), ("abc", None),
]


def claude_rows(exchanges=EXCHANGES, session=SESSION):
    rows, parent = [], None
    for n, (prompt, reply) in enumerate(exchanges, 1):
        rows.append({"type": "user", "uuid": f"u{n}", "parentUuid": parent, "isSidechain": False, "sessionId": session,
                     "message": {"role": "user", "content": prompt}})
        rows.append({"type": "assistant", "uuid": f"a{n}", "parentUuid": f"u{n}", "isSidechain": False, "sessionId": session,
                     "message": {"id": f"m{n}", "role": "assistant", "stop_reason": "end_turn",
                                 "content": [{"type": "text", "text": reply}]}})
        parent = f"a{n}"
    return rows


def codex_rows(exchanges=EXCHANGES, session=SESSION):
    rows = [{"type": "session_meta", "payload": {"id": session}}]
    for n, (prompt, reply) in enumerate(exchanges, 1):
        rows += [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(n)}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                       "content": [{"type": "input_text", "text": prompt}]}},
                 {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                                                       "content": [{"type": "output_text", "text": reply}]}},
                 {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": str(n), "last_agent_message": reply}}]
    return rows


def _command(provider, base, name, args, expected, transport, copied, pool="live", home=None):
    if provider == "claude":
        event = {**base, "hook_event_name": "UserPromptExpansion", "command_name": name, "command_args": args}
        typed = f"/{name}" + (f" {args}" if args else "")
    else:
        typed = f"${name}" + (f" {args}" if args else "")
        event = {**base, "hook_event_name": "UserPromptSubmit", "prompt": typed}
    answer = handle(event, transport)
    reply = answer["reason"] if answer else None
    if reply and home:  # the temp home differs per run: show a fixed path so the demo is reproducible
        reply = reply.replace(os.path.realpath(home), DEMO_HOME).replace(home, DEMO_HOME)
    return {"command": typed, "args": args, "blocked": bool(answer and answer.get("decision") == "block"),
            "reply": reply, "copied": copied[0] if copied else None,
            "expected": expected, "pool": pool}


def _write(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class _DemoClock(datetime):
    """Stored sessions in the demo are always dated 15 Jan 2026, so the output is reproducible."""
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def run_stored(provider: str) -> list[dict]:
    """history-use: a stored session as the source for this session, back to live, then a removed archive."""
    results = []
    clean = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**clean, "HOME": tmp}, clear=True), \
            patch("response_history.history_store.datetime", _DemoClock), \
            patch("response_history.clipboard.subprocess.run", side_effect=AssertionError("real clipboard used")):
        build = claude_rows if provider == "claude" else codex_rows
        current = _write(Path(tmp) / f"{SESSION}.jsonl", build())
        # The earlier session is still where its client keeps it, so history-use can show how to reopen it.
        native = Path(tmp) / (".claude/projects/demo" if provider == "claude" else ".codex/sessions")
        native.mkdir(parents=True)
        earlier = _write(native / f"{EARLIER_SESSION}.jsonl", build(EARLIER, EARLIER_SESSION))
        if provider == "claude":  # the client's own name for the earlier session (/rename or its automatic title)
            earlier.open("a").write(json.dumps({"type": "custom-title", "customTitle": "Login fix", "sessionId": EARLIER_SESSION}) + "\n")
        else:
            _write(Path(tmp) / ".codex/session_index.jsonl", [{"id": EARLIER_SESSION, "thread_name": "Login fix", "updated_at": "x"}])
        base = {"session_id": SESSION, "transcript_path": str(current), "cwd": tmp}
        base_earlier = {"session_id": EARLIER_SESSION, "transcript_path": str(earlier), "cwd": tmp}
        folder = Path(tmp).resolve() / ".agent-response-history" / "login-fix"

        def command(name, args, expected=None, pool="live", event=base):
            copied = []

            def transport(payload):
                copied.append(payload)
                return CopyResult("verified")
            results.append(_command(provider, event, name, args, expected, transport, copied, pool, tmp))

        results.append({"step": "Earlier, in the session the client named \"Login fix\": store it"})
        command("history-store", '--note "Safari reload bug"', event=base_earlier)

        results.append({"step": "Now, in this session: see the stored sessions and what history-list and history-copy use"})
        command("history-use", "list")
        results.append({"step": "Use the stored session login-fix in this session"})
        command("history-use", "login-fix", pool="stored")
        command("history-list", "", pool="stored")
        command("history-copy", "-2", [2, 3], "stored")
        command("history-copy", "9", pool="stored")
        command("history-use", "list", pool="stored")
        results.append({"step": "Switch back to this session's own replies"})
        command("history-use", "live")
        command("history-list", "2")
        command("history-use", "live")
        results.append({"step": "Use login-fix again, then delete its folder"})
        command("history-use", "login-fix", pool="stored")
        shutil.rmtree(folder)
        command("history-copy", "", pool="stored")
    return results


def run(provider: str, names=("history-list", "history-copy")) -> list[dict]:
    """Every ls/copy case through the hook; the clipboard is a recorder, the real one is blocked."""
    results = []
    with tempfile.TemporaryDirectory() as tmp, \
            patch("response_history.clipboard.subprocess.run", side_effect=AssertionError("real clipboard used")):
        transcript = Path(tmp) / f"{SESSION}.jsonl"
        rows = claude_rows() if provider == "claude" else codex_rows()
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        base = {"session_id": SESSION, "transcript_path": str(transcript), "cwd": tmp}
        for name, cases in ((names[0], [(a, None) for a in LIST_CASES]), (names[1], COPY_CASES)):
            for args, expected in cases:
                copied = []

                def transport(payload):
                    copied.append(payload)
                    return CopyResult("verified")

                results.append(_command(provider, base, name, args, expected, transport, copied))
    return results


def render(results: list[dict], stored: list[dict] = ()) -> str:
    """Markdown for docs/examples.md, deterministic for a fixed scenario."""
    out = ["# Examples", "",
           "Generated by `./test --examples` from a fixed fake session. Do not edit by hand.",
           "Every command below ran through the real hook code; the clipboard was a recorder,",
           "so running this never touches your own clipboard.", "",
           "## The fake session", "", "| # | Prompt | Reply |", "| --- | --- | --- |"]
    out += [f"| {n} | {p} | {r.replace(chr(10), ' ').replace('|', '&#124;')} |" for n, (p, r) in enumerate(EXCHANGES, 1)]
    section = None
    for r in results:
        name = r["command"].split()[0].lstrip("/$")
        if name != section:
            section = name
            out += ["", f"## {r['command'].split()[0]}", ""]
        out += ["```text", f"> {r['command']}", r["reply"], "```"]
        if r["copied"] is None:
            out += ["Clipboard: unchanged.", ""]
        else:
            numbers = ", ".join(f"#{i}" for i in r["expected"])
            how = ":" if len(r["expected"]) == 1 else ", in that order, separated by a blank line:"
            out += [f"Clipboard now holds {numbers}{how}", "", "```text", r["copied"], "```", ""]
    if stored:
        out += ["", "## Storing a session and using it later", "",
                "`/history-store` saves a session under the name its client gave it. In another session,",
                "`/history-use NAME` makes it the source for `/history-list` and `/history-copy` in that session",
                "only; `/history-use live` switches back. Nothing is resumed or sent to the model.", "",
                "The stored session login-fix:", "", "| # | Prompt | Reply |", "| --- | --- | --- |"]
        out += [f"| {n} | {p} | {r.replace('|', '&#124;')} |" for n, (p, r) in enumerate(EARLIER, 1)]
        out.append("")
        for r in stored:
            if "step" in r:
                out += [f"*{r['step']}.*", ""]
                continue
            out += ["```text", f"> {r['command']}", r["reply"], "```"]
            out += (["Clipboard: unchanged.", ""] if r["copied"] is None else
                    [f"Clipboard now holds #{', #'.join(map(str, r['expected']))} of login-fix:", "", "```text", r["copied"], "```", ""])
    out += ["In Codex the same commands start with `$` (for example `$history-copy -2`) and give the same results."]
    return "\n".join(out).rstrip() + "\n"


def render_terminal(results: list[dict], stored: list[dict] = ()) -> str:
    """The same results as render(), laid out for a terminal instead of a Markdown document."""
    out = ["FAKE SESSION (6 replies)", ""]
    out += [f"#{n}  {p}  ->  {r.splitlines()[0]}{' ...' if chr(10) in r else ''}" for n, (p, r) in enumerate(EXCHANGES, 1)]
    section = None
    for r in results:
        name = r["command"].split()[0].lstrip("/$")
        if name != section:
            section = name
            out += ["", "", name.replace("-", " ").upper()]
        out += ["", f"> {r['command']}", "", *r["reply"].splitlines(), ""]
        if r["copied"] is None:
            out.append("Clipboard: unchanged")
        else:
            out += ["Clipboard:", *[f"    {line}" if line else "" for line in r["copied"].splitlines()]]
    if stored:
        out += ["", "", "STORING A SESSION AND USING IT LATER (history-store, history-use)", ""]
        out += [f"login-fix #{n}  {p}  ->  {r}" for n, (p, r) in enumerate(EARLIER, 1)]
        for r in stored:
            if "step" in r:
                out += ["", f"--- {r['step']}"]
                continue
            out += ["", f"> {r['command']}", "", *r["reply"].splitlines(), ""]
            out += (["Clipboard: unchanged"] if r["copied"] is None else
                    ["Clipboard:", *[f"    {line}" if line else "" for line in r["copied"].splitlines()]])
    out += ["", "", "In Codex the same commands start with $ (for example $history-copy -2) and give the same results."]
    return "\n".join(out) + "\n"
