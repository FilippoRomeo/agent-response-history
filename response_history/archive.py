"""A stored session's machine-readable replies (turns.jsonl): normalized turns in, the same turns out.

replies.md is only for people to read; this file is what ls/copy use for a stored session.
Only the already-filtered prompt and reply of each selectable turn is written, never raw
transcript records.
"""
import json
from pathlib import Path
from response_history.model import Turn, UserError

TURNS = "turns.jsonl"
SCHEMA = 2


def dumps(turns) -> str:
    return "".join(json.dumps({"prompt": t.prompt, "response": t.text}, ensure_ascii=False) + "\n" for t in turns)


def load(folder: Path) -> tuple[list[Turn], dict]:
    """Every turn of the stored session, or UserError; never a partial list."""
    if not Path(folder).is_dir():
        raise UserError("its folder no longer exists")
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise UserError("its meta.json can't be read") from None
    if not isinstance(meta, dict):
        raise UserError("its meta.json is not valid")
    if meta.get("schema_version") == 1:
        raise UserError("it was stored before v2.2 and only has replies.md, not reusable replies")
    if meta.get("schema_version") != SCHEMA:
        raise UserError("it was stored in an unknown format")
    try:
        text = (folder / TURNS).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise UserError(f"its {TURNS} can't be read") from None
    turns = []
    # Split on "\n" only: JSON leaves U+2028/U+2029 unescaped and splitlines() would break on them.
    for line in text.split("\n")[:-1] if text.endswith("\n") else text.split("\n"):
        try:
            row = json.loads(line)
        except ValueError:
            raise UserError(f"its {TURNS} is damaged") from None
        if not isinstance(row, dict) or not isinstance(row.get("prompt"), str) or not isinstance(row.get("response"), str):
            raise UserError(f"its {TURNS} is damaged")
        turns.append(Turn(str(meta.get("provider") or "archive"), str(folder), parts=[row["response"]],
                          state="complete", evidence="archive", prompt=row["prompt"]))
    if len(turns) != meta.get("reply_count") or not turns:
        raise UserError(f"its {TURNS} doesn't match meta.json")
    return turns, meta
