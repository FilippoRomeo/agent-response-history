import json
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
    lines = text.splitlines(keepends=True)
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
