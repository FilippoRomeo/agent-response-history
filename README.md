# Agent Response History

Two things for Claude Code and Codex sessions on macOS:

- **Copy or list previous responses:** copy complete assistant responses from the current session to the clipboard, or list them with numbered previews.
- **Store and retrieve sessions:** save a named local archive of the current session, then list or look up archived sessions later, per project.

Exact supported commands run through a local hook before the model sees your prompt: no model request and no tokens.

## Install

```sh
git clone https://github.com/FilippoRomeo/agent-response-history.git
cd agent-response-history
./install.sh
```

`./install.sh` installs for every client it finds on your `PATH` (Claude Code, Codex, or both). To choose explicitly, run `./install.sh --provider claude`, `--provider codex` or `--provider both`. It needs Python 3.10 or newer and runs `install.py`, which you can also call directly with the same options, for example `python3 install.py --provider both`.

## Claude Code

Start a new Claude Code session, then:

```text
/copy-responses
/copy-responses -3
/copy-responses 5
/copy-responses 2,5-7

/ls-responses
/ls-responses 20

/store-history my-session
/store-history my-session "optional note"

/retrieve-history
/retrieve-history list
/retrieve-history my-session
```

## Codex

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

$store-history my-session
$store-history my-session "optional note"

$retrieve-history list
$retrieve-history my-session
```

Codex CLI note: a bare `$command` with no arguments, whether typed or pasted, opens the Codex composer's `$` picker, and Enter does not submit the message. This was observed with `$ls-responses`, `$copy-responses` and `$retrieve-history`. It is Codex composer behaviour, not a hook or model issue; commands with arguments are unaffected. Use the explicit forms instead: `$retrieve-history list`, `$ls-responses 10` (same as the default list) and `$copy-responses -1` (same as the default copy).

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

## Stored sessions

`store-history NAME` saves the current session as a local archive; `retrieve-history` finds it again later.

- **Names** use only `A-Z a-z 0-9 _ -`, 1 to 64 characters, and `list` is reserved. Any other name is refused and nothing is saved.
- **The note** is optional and goes on the same line in double quotes, for example `"Fixed the camera: "gamma" costs $5"`. The outer quotes are removed; everything between them is kept exactly as written.
- **Existing names are never overwritten.** Storing a name that already exists in the project is refused; there is no force or overwrite option. Saves are atomic: a failed save leaves nothing behind.
- **An archive is a snapshot** of the session at the moment you store it. A session with no complete response yet is not stored.
- **Durability:** if the archive was saved but the filesystem could not confirm it was written to disk, the reply still says `Stored …` and adds a warning. The archive is there; there is nothing to redo.

Archives live in `~/.local/share/agent-response-history-sessions/`, separate from the installed helper:

- **Scoped to the current project.** The project is the nearest folder, at or above the working directory, that contains `.git`; without one, it is the working directory itself. `retrieve-history` shows only the current project's archives.
- **Kept across updates.** Installer updates, `--uninstall` and `--rollback` never touch this folder. Uninstalling the integration does not delete stored history.

Each archive is a folder with `meta.json` (name, note, client, session ID, project, working directory, time, reply count, previews) and `replies.md`: the visible conversation, each of your prompts followed by the complete assistant reply.

### Retrieving

`retrieve-history` (in Codex, `$retrieve-history list`) shows the current project's archives, newest first, with client, reply count, date, and the note (or the first prompt when there is no note).

`retrieve-history NAME` shows the name, client, reply count, stored date, project, note or preview, whether the original session is still available, the path to the archived `replies.md`, and a command that reopens the original session:

```sh
cd '<original cwd>' && claude --resume '<session-id>'
cd '<original cwd>' && codex resume '<session-id>'
```

Copy the command that `retrieve-history` prints; it is already correctly quoted. If the original client session no longer exists, the detail says `Session: MISSING` and leaves out the reopen command. The archive and its `replies.md` are still there to read.

## What is left out

Copied responses and stored archives contain only the visible conversation. The transcript parsers leave out what they recognise as internal or client material:

- hidden reasoning and thinking;
- tool calls and tool results;
- developer, system and meta records, sidechains and synthetic client notices;
- unfinished or aborted assistant replies;
- turns that ran this project's own commands;
- known context that the client injects into prompts, such as `AGENTS.md` instructions, environment context and IDE context.

This covers the record types produced by the tested client versions; a future client version may add records the parsers do not yet recognise. Text you write yourself is kept, including documents such as `<svg>…</svg>` or `<config>…</config>`.

## Token use

Exact supported commands are handled by the hook before model inference. In tests with real Claude Code 2.1.280 and Codex CLI 0.156.0 and 0.156.1, the intercepted commands produced no model-generated assistant turn and used 0 tokens. The clients may still record a little local metadata about the intercepted prompt in their own session files.

The message does reach the model, and uses tokens like any other prompt, when:

- the command is mistyped (`$ls-reponses`);
- the command is inside other text ("please run /copy-responses");
- the Codex hook has not been trusted in `/hooks` yet, or Codex asks to trust it again after an update;
- the client does not run the hook (see [Editors](#editors)).

## How it works

The installer puts one shared Python helper in `~/.local/share/agent-response-history/` and registers it as a prompt hook:

- Claude Code: a `UserPromptExpansion` hook in `~/.claude/settings.json`, plus four command files in `~/.claude/commands/` (`copy-responses`, `ls-responses`, `store-history`, `retrieve-history`).
- Codex: a `UserPromptSubmit` hook in `~/.codex/hooks.json`. No Codex skills or prompt files are installed.

The hook reads the current session's local transcript and picks out the complete visible replies. Then:

- **copy/list:** `copy-responses` copies the selected responses with `pbcopy` and confirms the copy with `pbpaste`; `ls-responses` only prints previews and never writes the clipboard.
- **store/retrieve:** `store-history` writes an archive under `~/.local/share/agent-response-history-sessions/`; `retrieve-history` reads only the current project's archives. Neither uses the clipboard.

Nothing is sent over the network. Existing settings and hooks are kept.

## Requirements

- macOS: copying uses `pbcopy` and `pbpaste`, and storing sessions currently requires macOS
- Python 3.10 or newer
- Tested with Claude Code 2.1.280 and Codex CLI 0.156.1. Transcript formats can change between client versions.

## Editors

Use the commands in the Claude Code and Codex terminal apps.

- **Claude Code for VS Code:** not supported. The extension (tested with 2.1.277) does not run the hook, so the command does nothing and Claude replies that the hook did not run. That reply is one short model turn.
- **Codex IDE extension and desktop app:** not tested.

If the hook does not run, a command never reports a copy, listing, store or retrieval that did not happen. The client may still show the command's arguments (for example a session name or note) to the model as part of that one reply.

## Update

```sh
git pull
./install.sh
```

Re-running the installer is safe; it changes only what differs. `./install.sh` reuses the Python that your existing hook runs with, so the hook command, and therefore Codex's trust, stays the same. If the hook command does change, for example because that Python no longer exists or you run `install.py` with a different one, Codex asks you to trust it again in `/hooks`.

## Uninstall and rollback

```sh
./install.sh --uninstall
```

Every install or uninstall that changes something prints a backup folder under `~/.local/share/agent-response-history-backups/`. To undo that run exactly:

```sh
./install.sh --rollback ~/.local/share/agent-response-history-backups/<folder>
```

`python3 install.py --uninstall` and `python3 install.py --rollback …` do the same.

Files are moved, never deleted. Command files you have changed or written yourself are left alone.

Stored sessions in `~/.local/share/agent-response-history-sessions/` are your data, not part of the installation. Install, update, uninstall and rollback leave them untouched and do not copy them into the backup folder. Delete that folder yourself if you no longer want the archives.

## Replacing the older projects

This project replaces [claude-response-history](https://github.com/FilippoRomeo/claude-response-history) and [codex-response-history](https://github.com/FilippoRomeo/codex-response-history). The installer recognises their installed command, skill and prompt files and moves them into the backup folder, so only one `copy-responses` and one `ls-responses` stay active. Files it does not recognise are left alone and reported. The older projects had no stored sessions; that feature is new here.

| | Older projects | agent-response-history |
| --- | --- | --- |
| Clients | Two repositories, one per client | One install for Claude Code, Codex, or both |
| Running a command | Every command was a model turn, so it used tokens | A local hook answers before the model; 0 tokens |
| Copying | No read-back check | Copies with `pbcopy`, then reads back with `pbpaste` and compares byte for byte |
| Install | One script per project; refused to replace an existing install | One installer that moves old installs aside, updates in place, and can `--uninstall` or `--rollback` |

## Development

```sh
python3 -m unittest discover -s tests
```

MIT licensed.
