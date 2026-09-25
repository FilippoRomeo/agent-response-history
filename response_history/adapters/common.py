import io
import json
import re
from pathlib import Path
from response_history.model import TranscriptError


def records(path: Path):
    """Read one bounded snapshot; tolerate only a syntactically unfinished tail."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data" or exc.end != len(data):
            raise TranscriptError("Invalid transcript UTF-8") from None
        text = data[:exc.start].decode("utf-8")
    # JSONL records end at "\n" only; splitlines() also breaks on U+2028/U+2029/U+0085, which JSON leaves raw.
    lines = list(io.StringIO(text, newline="\n"))
    result = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            trailing = index == len(lines) - 1 and not line.endswith("\n")
            unfinished = exc.pos >= len(line.rstrip("\r\n")) and exc.msg in (
                "Expecting value", "Expecting property name enclosed in double quotes",
                "Expecting ',' delimiter", "Expecting ':' delimiter",
            ) or exc.msg.startswith("Unterminated string") and trailing
            if trailing and unfinished:
                break
            raise TranscriptError(f"Invalid transcript JSON at line {index + 1}") from None
        if not isinstance(row, dict):
            raise TranscriptError(f"Invalid transcript record at line {index + 1}")
        result.append(row)
    return result


def visible_parts(content):
    if not isinstance(content, list):
        raise TranscriptError("Invalid assistant content")
    parts = []
    for item in content:
        if not isinstance(item, dict):
            raise TranscriptError("Invalid assistant content block")
        kind = item.get("type")
        if kind in ("output_text", "text"):
            if not isinstance(item.get("text"), str):
                raise TranscriptError("Invalid assistant text")
            if item["text"]:
                parts.append(item["text"])
        elif kind in ("thinking", "redacted_thinking", "tool_use", "tool_result"):
            continue
        else:
            raise TranscriptError("Unsupported assistant content type")
    return parts


# Client-injected context arrives as user-role text that is exactly one known block.
# Only tags observed as client-generated in real Codex/Claude transcripts are listed;
# any other tag document (<svg>, <config>, ...) is human input and is kept.
_INJECTED_TAGS = (
    "environment_context", "user_instructions", "recommended_plugins", "skill", "turn_aborted",
    "guardian_tool_descriptions", "guardian_context_omission", "external_codex_apps_writing_block_edits",
    "ide_opened_file", "ide_selection", "local-command-stdout", "local-command-stderr", "task-notification",
)
_INJECTED = re.compile(r"\s*<(%s)>.*</\1>\s*" % "|".join(map(re.escape, _INJECTED_TAGS)), re.S)
_IDE_REQUEST = "\n## My request for Codex:\n"


def prompt_text(content) -> str:
    """Human-typed text of one user record, without client-injected context."""
    if isinstance(content, str):
        items = [content]
    elif isinstance(content, list):
        items = [x.get("text") for x in content if isinstance(x, dict) and x.get("type") in ("text", "input_text")]
    else:
        items = []
    kept = []
    for item in items:
        if not isinstance(item, str) or not item.strip() or _INJECTED.fullmatch(item) or item.startswith("# AGENTS.md instructions"):
            continue
        if item.startswith("# Context from my IDE setup:") and _IDE_REQUEST in item:
            item = item.rpartition(_IDE_REQUEST)[2]
        kept.append(item)
    return "\n\n".join(kept)
