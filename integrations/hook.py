"""Prompt-interception entry point for Codex (UserPromptSubmit) and Claude Code (UserPromptExpansion).

Both clients hand the exact command to this script before any model turn; the
result is returned as a block/deny reason, so no model inference happens.
"""

import contextlib
import io
import json
import re
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from response_history.cli import main
from response_history import history_store

COMMANDS = ("copy-responses", "ls-responses")
HISTORY_COMMANDS = ("store-history", "retrieve-history")  # never touch the clipboard
CODEX_COMMAND = re.compile(r"^\$(copy-responses|ls-responses|store-history|retrieve-history)(?=$|[ \t\r\n])")


def _session(event: dict):
    session = event.get("session_id")
    transcript = event.get("transcript_path")
    try:
        if str(uuid.UUID(session)) != session:
            raise ValueError
        if not isinstance(transcript, str) or not Path(transcript).is_absolute():
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        return None
    return session, transcript


def _history(provider: str, name: str, args: str, event: dict) -> str:
    found, cwd = _session(event), event.get("cwd")
    if found is None or not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return "Response history session unavailable"
    return history_store.run(provider, name, args, found[0], found[1], cwd)


def _run(provider: str, name: str, selector: str, event: dict, transport) -> str:
    if name in HISTORY_COMMANDS:
        return _history(provider, name, selector, event)
    found = _session(event)
    if found is None:
        return "Response history session unavailable"
    session, transcript = found
    if not Path(transcript).exists():  # Claude writes the transcript only after the first exchange
        return "No responses yet."
    action = "copy" if name == "copy-responses" else "list"
    argv = ["--provider", provider, "--file", transcript, "--session", session, "--", action]
    if selector:
        argv.append(selector)
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        status = main(argv, **({"transport": transport} if transport else {}))
    reason = output.getvalue().strip() if status == 0 else errors.getvalue().strip()
    return reason or "Response history unavailable"


def handle(event: dict, transport=None) -> dict | None:
    kind = event.get("hook_event_name")
    if kind == "UserPromptSubmit":  # Codex: prompt is the raw "$command args" text
        prompt = event.get("prompt")
        match = CODEX_COMMAND.match(prompt) if isinstance(prompt, str) else None
        if match is None:
            return None
        tail = prompt[match.end():]
        if "\n" in tail or "\r" in tail:
            return {"decision": "block", "reason": "Invalid response-history arguments"}
        reason = _run("codex", match.group(1), tail.strip(), event, transport)
        return {"decision": "block", "reason": reason}
    if kind == "UserPromptExpansion":  # Claude: slash args arrive as command_args
        name = event.get("command_name")
        if name not in COMMANDS + HISTORY_COMMANDS:
            return None
        selector = event.get("command_args")  # Claude Code sends args here; expanded_prompt is absent
        selector = selector.strip() if isinstance(selector, str) else ""
        reason = _run("claude", name, selector, event, transport) if "\n" not in selector else "Invalid response-history arguments"
        return {"decision": "block", "reason": reason}  # permissionDecision is ignored for this event
    return None


if __name__ == "__main__":
    result = handle(json.load(sys.stdin))
    if result is not None:
        print(json.dumps(result))
