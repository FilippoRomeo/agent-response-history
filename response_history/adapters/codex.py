import re
from response_history.model import Turn, TranscriptError
from response_history.adapters.common import prompt_text, visible_parts


_HELPER = re.compile(r"^(?:\$(?:copy-responses|ls-responses|store-history|retrieve-history|history-list|history-copy|history-store|history-use)|/(?:prompts:)?(?:copy-responses|ls-responses|copy-response|copy|store-history|retrieve-history))(?:\s|$)")
_NON_TEXT_EVENTS = {
    "item_started", "token_count", "thread_settings_applied",
    "exec_command_begin", "exec_command_output_delta", "exec_command_end",
    "mcp_tool_call_begin", "mcp_tool_call_end", "web_search_begin", "web_search_end",
}


def parse(rows, source: str) -> list[Turn]:
    turns = []
    active = None
    explicit = False
    event_messages = {}
    response_messages = {}
    for row in rows:
        kind = row.get("type")
        data = row.get("payload")
        if kind in ("session_meta", "turn_context", "world_state", "token_usage_record"):
            continue
        if not isinstance(data, dict):
            raise TranscriptError("Invalid Codex payload")
        name = data.get("type")
        if kind == "event_msg":
            if name in ("session_rollback", "thread_rollback", "thread_rolled_back", "history_rewrite", "context_compacted"):
                raise TranscriptError("Unsupported Codex history rewrite")
            if name in ("task_started", "turn_started"):
                if active is not None:
                    active.state = "incomplete" if explicit or active.evidence != "final_item" else "complete"
                active = Turn("codex", source, data.get("turn_id"))
                explicit = True
                event_messages, response_messages = {}, {}
                turns.append(active)
            elif name == "turn_aborted":
                if active is not None:
                    active.state = "aborted"
                    active = None
                    explicit = False
            elif name in ("task_complete", "turn_complete"):
                if active is None:
                    continue
                if active.identity and data.get("turn_id") and active.identity != data["turn_id"]:
                    raise TranscriptError("Mismatched Codex turn identity")
                if any(response_messages.get(key) != value for key, value in event_messages.items()):
                    raise TranscriptError("Unmirrored Codex agent message")
                if data.get("error") is not None:
                    active.state = "aborted"
                    active = None
                    explicit = False
                    continue
                fallback = data.get("last_agent_message")
                if fallback is not None and not isinstance(fallback, str):
                    raise TranscriptError("Invalid Codex completion text")
                if fallback:
                    if active.evidence == "final_item":
                        if active.parts[-1] != fallback:
                            raise TranscriptError("Mismatched Codex final record")
                    else:
                        active.parts.append(fallback)
                active.state = "complete" if active.evidence == "final_item" or fallback else "ambiguous"
                active.evidence = "task_complete"
                active = None
                explicit = False
            elif name == "item_completed":
                item = data.get("item")
                if not isinstance(item, dict):
                    raise TranscriptError("Invalid Codex completed item")
                if item.get("type") == "AgentMessage":
                    identity = item.get("id")
                    content = item.get("content")
                    if active is None or not isinstance(identity, str) or not isinstance(content, list):
                        raise TranscriptError("Unsupported Codex agent item")
                    if any(not isinstance(part, dict) or part.get("type") != "Text" or not isinstance(part.get("text"), str) for part in content):
                        raise TranscriptError("Unsupported Codex agent item content")
                    if identity in event_messages:
                        raise TranscriptError("Duplicate Codex agent item")
                    event_messages[identity] = (item.get("phase"), "\n".join(part["text"] for part in content))
            elif name not in _NON_TEXT_EVENTS:
                raise TranscriptError("Unsupported Codex event type")
            continue
        if kind != "response_item":
            continue
        if name != "message":
            continue
        role = data.get("role")
        if role == "user":
            content = data.get("content", [])
            if isinstance(content, list):
                prompt = "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "input_text")
                if not prompt.strip():
                    continue
                if active is None or active.evidence == "final_item":
                    if active is not None:
                        if any(response_messages.get(key) != value for key, value in event_messages.items()):
                            raise TranscriptError("Unmirrored Codex agent message")
                        active.state = "complete"
                    active = Turn("codex", source, active.identity if active is not None and explicit else None)
                    event_messages, response_messages = {}, {}
                    turns.append(active)
                active.prompt = "\n\n".join(filter(None, (active.prompt, prompt_text(content))))  # steering joins its turn
                if _HELPER.match(prompt.lstrip()) or "<name>copy-responses</name>" in prompt or "<name>ls-responses</name>" in prompt or "<!-- codex-response-history:" in prompt:
                    active.excluded = "helper command"
        elif role == "assistant":
            if active is None:
                continue
            phase = data.get("phase")
            if phase not in (None, "commentary", "final", "final_answer"):
                raise TranscriptError("Unsupported Codex visible phase")
            parts = visible_parts(data.get("content", []))
            if parts:
                active.parts.append("\n".join(parts))
            if isinstance(data.get("id"), str):
                response_messages[data["id"]] = (phase, "\n".join(parts))
            if phase in ("final", "final_answer") and parts:
                if active.evidence == "final_item":
                    raise TranscriptError("Duplicate Codex final record")
                active.evidence = "final_item"
                if not explicit:
                    active.state = "complete"
    return turns
