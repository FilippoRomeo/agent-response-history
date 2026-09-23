from response_history.model import Turn, TranscriptError
from response_history.adapters.common import visible_parts


def _helper(content) -> bool:
    if not isinstance(content, str):
        return False
    return any(f"<command-name>/{name}</command-name>" in content
               for name in ("copy-responses", "ls-responses", "copy-response", "copy"))


def _tool_result(content) -> bool:
    return isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)


def _human_ancestor(row, seen):
    parent = row.get("parentUuid")
    visited = set()
    while isinstance(parent, str):
        if parent in visited:
            break
        visited.add(parent)
        ancestor = seen.get(parent)
        if ancestor is None or ancestor.get("isSidechain"):
            break
        if ancestor.get("type") == "user" and not ancestor.get("isMeta") and not _tool_result((ancestor.get("message") or {}).get("content")):
            return parent
        parent = ancestor.get("parentUuid")
    raise TranscriptError("Unsupported Claude turn ancestry")


def parse(rows, source: str) -> list[Turn]:
    turns = []
    active = None
    session = None
    pending_tools = set()
    completed_id = None
    seen = {}
    for row in rows:
        identity = row.get("uuid")
        if identity is not None:
            if not isinstance(identity, str) or identity in seen:
                raise TranscriptError("Ambiguous Claude record identity")
            seen[identity] = row
        kind = row.get("type")
        if row.get("isSidechain"):
            continue
        sid = row.get("sessionId")
        if sid is not None:
            if session is not None and sid != session:
                raise TranscriptError("Mixed Claude session identities")
            session = sid
        if kind not in ("user", "assistant"):
            continue  # ponytail: only user/assistant carry responses; Claude Code adds new metadata types constantly
        if row.get("isMeta"):
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            raise TranscriptError("Invalid Claude message")
        if kind == "assistant" and message.get("model") == "<synthetic>":
            continue  # client-generated notice (resume filler, API/limit errors), never a model response
        content = message.get("content")
        if kind == "user":
            if _tool_result(content):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        pending_tools.discard(item.get("tool_use_id"))
                continue
            if active is not None and active.state == "active":
                active.state = "incomplete"
            active = Turn("claude", source, row.get("uuid"))
            pending_tools.clear()
            if _helper(content):
                active.excluded = "helper command"
            turns.append(active)
            continue
        if active is None:
            # A visible assistant with no human turn cannot be numbered safely.
            raise TranscriptError("Orphan Claude assistant message")
        if _human_ancestor(row, seen) != active.identity:
            raise TranscriptError("Claude assistant belongs to another turn")
        if active.state == "complete":
            # Claude Code splits one API message into several records sharing message.id,
            # each stamped with the final stop_reason; same id after completion is a continuation.
            if message.get("id") != completed_id:
                continue  # reply to a meta prompt (e.g. another session's message): not numbered
        elif active.state != "active":
            continue
        if not isinstance(content, list):
            raise TranscriptError("Invalid Claude assistant content")
        parts = visible_parts(content)
        if parts:
            active.parts.append("\n".join(parts))
        for item in content:
            if item.get("type") == "tool_use":
                if not isinstance(item.get("id"), str):
                    raise TranscriptError("Claude tool use lacks identity")
                pending_tools.add(item["id"])
        stop = message.get("stop_reason")
        if stop == "end_turn":
            active.state = "complete" if not pending_tools else "unsupported"
            completed_id = message.get("id")
            active.evidence = "stop_reason=end_turn"
        elif stop not in (None, "tool_use"):
            active.state = "unsupported"
            active.evidence = "unknown stop reason"
    return turns
