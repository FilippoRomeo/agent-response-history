# Agent Response History

Copy complete previous assistant responses from the current Claude Code or Codex session to the macOS clipboard, or list them with numbered previews.

The commands run locally before the model sees your prompt: no model request, no tokens, and the copied text never enters the conversation.

## Claude Code

```sh
git clone https://github.com/FilippoRomeo/agent-response-history.git
cd agent-response-history
python3 install.py --provider claude
```

Start a new Claude Code session, then:

```text
/copy-responses
/copy-responses -3
/copy-responses 5
/copy-responses 2,5-7

/ls-responses
/ls-responses 20
```

## Codex

```sh
git clone https://github.com/FilippoRomeo/agent-response-history.git
cd agent-response-history
python3 install.py --provider codex
```

Codex runs a new hook only after you approve it:

1. Start or restart Codex.
2. Run `/hooks`.
3. Review and trust the `agent-response-history` `UserPromptSubmit` hook.

Then:

```text
$copy-responses
$copy-responses -3
$copy-responses 5
$copy-responses 2,5-7

$ls-responses
$ls-responses 20
```

## Install both

```sh
python3 install.py --provider both
```

## Selectors

| Selector | Copies |
| --- | --- |
| *(none)* | The latest complete response |
| `-3` | The latest 3 responses, oldest first (`-1` to `-10`) |
| `5` | Response 5 |
| `2,5` | Responses 2 and 5, in that order |
| `5-7` | Responses 5, 6 and 7 (inclusive) |
| `2,5-7` | Any mix of numbers and ranges |

`ls-responses` shows the latest 10 previews; `ls-responses N` shows the latest N. Numbers are stable for the session, so list first, then copy by number.

Responses are joined with a blank line. An invalid or out-of-range selection shows an error and leaves the clipboard unchanged. Only the exact command at the start of a message is intercepted; anything else, including a typo, goes to the model as usual.

## How it works

The installer puts one shared Python helper in `~/.local/share/agent-response-history/` and registers it as a prompt hook:

- Claude Code: a `UserPromptExpansion` hook in `~/.claude/settings.json`, plus the two command files in `~/.claude/commands/`.
- Codex: a `UserPromptSubmit` hook in `~/.codex/hooks.json`.

The hook reads the current session's local transcript, picks complete visible replies (no reasoning, tool output, or unfinished turns), copies with `pbcopy`, and confirms the copy with `pbpaste`. Nothing is sent over the network. Existing settings and hooks are kept.

## Requirements

- macOS (uses `pbcopy` and `pbpaste`)
- Python 3.10 or newer
- Tested with Claude Code 2.1.280 and Codex CLI 0.156.0. Transcript formats can change between client versions.

## Update

```sh
git pull
python3 install.py --provider both   # or claude / codex
```

Re-running the installer is safe; it changes only what differs. If the hook command changes, Codex asks you to trust it again in `/hooks`.

## Uninstall and rollback

```sh
python3 install.py --uninstall
```

Every install or uninstall that changes something prints a backup folder under `~/.local/share/agent-response-history-backups/`. To undo that run exactly:

```sh
python3 install.py --rollback ~/.local/share/agent-response-history-backups/<folder>
```

Files are moved, never deleted.

## Replacing the older projects

This project replaces [claude-response-history](https://github.com/FilippoRomeo/claude-response-history) and [codex-response-history](https://github.com/FilippoRomeo/codex-response-history). The installer recognises their installed command, skill and prompt files and moves them into the backup folder, so only one `copy-responses` and one `ls-responses` stay active. Files it does not recognise are left alone and reported.

## Development

```sh
python3 -m unittest discover -s tests
```

MIT licensed.
