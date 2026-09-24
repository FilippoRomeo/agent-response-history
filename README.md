# Agent Response History

List, copy, store and reuse assistant replies from Claude Code and Codex sessions on macOS, with four commands:

- **`history-list`** lists the replies of the current source, numbered.
- **`history-copy`** copies replies to the clipboard.
- **`history-store`** saves the current session as a local archive.
- **`history-use`** makes a stored archive the source for list and copy, or switches back.

By default the source is the session you are in (LIVE). A stored archive can take its place:

```text
LIVE session ──────────────────────────► history-list / history-copy

history-store ─────► stored archive

history-use NAME or PATH ──► the stored archive becomes the source for history-list / history-copy
history-use live ──────────► back to the LIVE session
```

The commands are answered by a local hook on your machine, before the prompt reaches the model.

## Install

```sh
git clone https://github.com/FilippoRomeo/agent-response-history.git
cd agent-response-history
./install.sh
```

`./install.sh` installs for every client it finds on your `PATH`. To choose explicitly:

```sh
./install.sh --provider claude
./install.sh --provider codex
./install.sh --provider both
```

It needs macOS and Python 3.10 or newer. It runs `install.py`, which you can also call directly with the same options.

**Claude Code:** start a new session after installing.

**Codex:** a new hook runs only after you approve it. Start or restart Codex, run `/hooks`, then review and trust the `agent-response-history` `UserPromptSubmit` hook.

Installing over an earlier version replaces its commands (`copy-responses`, `ls-responses`, `store-history`, `retrieve-history`) with the `history-*` family. The old command files are recognised and moved into the installer's backup folder; there is nothing to delete by hand. See [Update](#update).

After upgrading, use only the `history-*` commands. The old names are not aliases: agent-response-history no longer handles them, and the client treats them like any other input.

## Commands

| Command | Claude Code | Codex | What it does |
| --- | --- | --- | --- |
| `history-list` | `/history-list` | `$history-list` | List numbered replies from the current source |
| `history-copy` | `/history-copy` | `$history-copy` | Copy selected replies to the clipboard |
| `history-store` | `/history-store` | `$history-store` | Save the current session as a local archive |
| `history-use` | `/history-use` | `$history-use` | List archives, select one, or return to LIVE |

Only an exact command at the start of a message is handled. Anything else, including a typo or a command inside other text, is left to the client as usual.

In Codex, give each command an argument, for example `$history-list 10`, `$history-copy -1`, `$history-use list` and `$history-store home`. These are sent on the first Enter.

A bare command with no arguments, such as `$history-list`, `$history-copy` or `$history-use`, opens the Codex composer's `$` picker showing "no matches", and Enter does not send it. Press Esc to close the picker: the command stays in the composer, and Enter then sends it to the hook. This is how the Codex composer handles `$` (observed with Codex CLI 0.156.1), not something this tool does.

Exact output for every example below, generated from a fixed fake session, is in [docs/examples.md](docs/examples.md).

## List and copy

```text
history-list           the latest 10 replies
history-list 5         the latest 5
history-list -5        the same as 5

history-copy           the latest complete reply
history-copy -3        the latest 3, oldest first (-1 to -10)
history-copy 5         reply 5
history-copy 2,5       replies 2 and 5, in that order
history-copy 5-7       replies 5, 6 and 7
history-copy 2,5-7     any mix of numbers and ranges
```

- Numbers are stable for a session, so list first, then copy by number.
- Replies are copied in the order you asked for and joined with one blank line.
- The copy is checked by reading the clipboard back.
- An invalid or out-of-range selection explains what was wrong and leaves the clipboard unchanged.

When a stored archive is the source, the output says so: `Stored session: login-fix` above the list, and `Copied #2, #3 from stored session login-fix` after a copy.

## Store a session

```text
/history-store [home | PATH] [--note TEXT] [--name NAME]
$history-store [home | PATH] [--note TEXT] [--name NAME]
```

This saves a snapshot of the current session at that moment. A session with no complete reply yet is not stored. The reply shows where the archive went and the exact `history-use` command to reuse it later.

### Where archives go

| Command | Archive folder |
| --- | --- |
| `history-store` | `<current folder>/.agent-response-history/<name>/` |
| `history-store home` | Claude Code: `${CLAUDE_CONFIG_DIR:-~/.claude}/agent-response-history/<name>/`<br>Codex: `${CODEX_HOME:-~/.codex}/agent-response-history/<name>/` |
| `history-store PATH` | `PATH/<name>/` |

The current folder is the session's working directory exactly, not a parent or the Git root.

For `history-store PATH`:

- a relative path starts from the session's working directory, and `~` means your home folder;
- quote a path that contains spaces: `/history-store "~/My Archives"`;
- if the folder doesn't exist but its parent does, it is created;
- if the parent doesn't exist, or the path is a file, nothing is stored.

A folder called `home` is written `./home`.

### Names

Without `--name`, the archive is named after the session's own title:

- **Claude Code:** the name you gave with `/rename`, otherwise Claude's automatic title.
- **Codex:** the thread's current name.

The title becomes a lowercase folder name:

```text
Fix login form reload (Safari)  →  fix-login-form-reload-safari
Café menu v2                    →  cafe-menu-v2
```

- If nothing usable is left, for example with a title only in Japanese, or there is no title yet, the name is `<client>-<date>-<first 8 characters of the session ID>`, such as `claude-2026-09-24-3f2a91c0`.
- If the name is already taken, `-2`, `-3` and so on is added.
- The original title is kept in the archive's metadata.

`--name NAME` sets the name yourself: 1 to 64 characters from `A-Z a-z 0-9 _ -`.

```text
/history-store --name login-fix
```

An explicit name is never changed. If it is already taken, nothing is stored. `list` and `live` are reserved in any capitalisation.

### Note

```text
/history-store --note "fixed Safari reload"
```

The note is a description shown by `history-use list`. It never changes the name, the folder or the stored replies.

### What an archive contains

```text
<name>/
├── meta.json      name, title, note, client, session ID, working directory, date, reply count, previews
├── turns.jsonl    your prompts and the complete replies, used by history-list and history-copy
├── replies.md     the same conversation as a readable document
└── .gitignore     keeps the archive out of Git
```

- Folders are private to your user; files are readable only by you.
- Existing archives are never overwritten, and a failed save leaves nothing behind.
- If the archive was written but the filesystem could not confirm it reached the disk, the reply still says `Stored …` and adds a warning.

### Git and privacy

An archive in a project folder contains your conversation. Its own `.gitignore` keeps it out of `git status` and `git add -A`, and nothing is added to your project's `.gitignore`.

Ignored files are still files. **`git clean -X` and `git clean -x` delete ignored files, including `.agent-response-history` archives.** Use `history-store home` or a path outside the project if that matters to you.

Archives stay on your machine; this tool never uploads them anywhere.

## Use a stored archive

```text
/history-use list      the current source and the archives found here
/history-use NAME      use an archive found by list
/history-use PATH      use the archive folder at PATH
/history-use live      back to the current session
```

In Claude Code, a bare `/history-use` is the same as `/history-use list`. In Codex, write `$history-use list`.

**`history-use list`** shows the current source (`Current source: LIVE` or `Current source: stored session NAME`). It then lists the archives in exactly three places:

1. `.agent-response-history` in the current folder (`here`);
2. the current client's home store (`home`);
3. archives stored by v2.1 for this project (`v2.1`).

Nothing else is searched: no parent folders, no other client's home store, and no index of places you stored before. An archive stored at an explicit PATH is reused by its path.

**`history-use NAME`** selects one of those archives. From then on, `history-list` and `history-copy` read the archive, and their output is labelled as a stored session. If the same name exists in two places, nothing is selected and both paths are shown; choose one with `history-use PATH`.

**`history-use PATH`** selects an archive folder directly, wherever it is. The `history-store` reply prints this command for you.

**`history-use live`** returns list and copy to the current session.

The choice belongs to the Claude Code or Codex session you made it in; other sessions keep their own. It stays when you resume that same session later. If the selected archive is moved or deleted, list and copy say so and do nothing; they never quietly fall back to the live session.

### Reopening the original session

An archive keeps working after its original session is gone. If that session is still on this machine, `history-use` also shows the command that reopens it, for example:

```sh
cd '/Users/you/login-app' && claude --resume '<session-id>'
cd '/Users/you/login-app' && codex resume '<session-id>'
```

You run it yourself in a terminal. `history-use` never runs it, never opens a terminal, and never sends the archive to the model.

### Archives from the other client

Archives in a folder or at a PATH work from either client: a Claude Code session can list and copy a stored Codex session, and the other way round. The reopen command always uses the client the archive came from. Only the current client's own home store appears in `history-use list`; the other one is reachable by path.

### Archives from v2.1

v2.1 stored sessions in `~/.local/share/agent-response-history-sessions/`, per project. They are still there and still appear in `history-use list`, marked `v2.1` and `[v2.1 format]`.

That format has only `meta.json` and `replies.md`, not the replies that list and copy need. So these archives can't become the source for `history-list` or `history-copy`. They are not converted and `replies.md` is not parsed back into replies. If the original session still exists, `history-use NAME` still shows how to reopen it, and `replies.md` stays readable. New archives are never written to this folder.

## What is stored and what is left out

Listing, copying and storing use the visible conversation only: your prompts and each complete assistant reply. The transcript readers leave out what they recognise as internal or client material:

- hidden reasoning and thinking;
- tool calls and tool results;
- system, developer and meta records, sidechains and synthetic client notices;
- unfinished or aborted replies;
- the turns that ran this tool's commands, current and earlier names alike;
- context the client injects into prompts, such as `AGENTS.md` or `CLAUDE.md` instructions, environment context and IDE context.

This covers the records written by the client versions it was built against; a future client version may add records the readers don't yet recognise. Text you write yourself is kept, including documents such as `<svg>…</svg>`.

An archive holds that conversation, not the session's settings, memory, instruction files or other client context.

## How commands are handled

```text
Claude Code or Codex command → local hook → local transcript or archive → list / copy / store / use
```

- **Claude Code:** a `UserPromptExpansion` hook in `~/.claude/settings.json`, and four command files in `~/.claude/commands/`.
- **Codex:** a `UserPromptSubmit` hook in `~/.codex/hooks.json`. No Codex skills or prompt files are installed.

Both run one shared helper in `~/.local/share/agent-response-history/`. Copying uses `pbcopy` and checks the result with `pbpaste`. There is no daemon, no service, and no database or index of archives. Nothing is sent over the network.

When the hook handles a command, the reply comes from the hook and needs no model inference. When it doesn't:

- **Claude Code:** the command file tells the model to answer only that the hook did not run and nothing was done. That answer is one short model turn.
- **Codex:** the text is sent to the model like any other prompt, for example while the hook is not yet trusted in `/hooks`, or after an update asks you to trust it again.

## Clients

This tool is built for the Claude Code and Codex terminal apps. Tested with Claude Code 2.1.282 and Codex CLI 0.156.1: in those terminal-client tests, every supported `history-*` command intercepted by the hook added no model-generated assistant turn and no model token usage. That holds only when the hook intercepts the command; see [How commands are handled](#how-commands-are-handled).

Editor extensions and desktop apps are not part of the tested workflow.

## Update

```sh
git pull
./install.sh
```

Re-running the installer changes only what differs. It reuses the Python your existing hook runs with, so the hook command, and Codex's trust in it, stays the same. If the hook command does change, Codex asks you to trust it again in `/hooks`.

When updating from v2.1 or earlier, the old command files are recognised by their exact content and moved into the backup folder. Command files you wrote or changed are never overwritten. If a file under one of the old command names isn't recognised, the update stops before changing anything and says which file to move away.

## Uninstall and rollback

```sh
./install.sh --uninstall
```

Every install or uninstall that changes something prints a backup folder. To undo that run exactly:

```sh
./install.sh --rollback ~/.local/share/agent-response-history-backups/<folder>
```

Files are moved into the backup folder, never deleted.

**Archives are never touched** by install, update, uninstall or rollback: not in project folders, not in the home stores, not at other paths, and not in the v2.1 folder. Delete them yourself when you no longer want them.

**The current source for each session** is kept in `~/.local/state/agent-response-history/`. This is the tool's working state, not your archives:

- an update keeps it;
- an uninstall moves it into the backup folder;
- rolling back an install or update moves it into the rollback's backup, since an older version doesn't understand it;
- rolling back an uninstall restores it.

## Replacing the older projects

This project replaces [claude-response-history](https://github.com/FilippoRomeo/claude-response-history) and [codex-response-history](https://github.com/FilippoRomeo/codex-response-history). The installer recognises their command, skill and prompt files and moves them into the backup folder. Other files it doesn't recognise are left alone and reported, except under the old command names, where an unrecognised file stops the install (see [Update](#update)).

## Requirements

- macOS. Copying uses `pbcopy` and `pbpaste`, and archives are published with an atomic macOS rename that never replaces an existing folder.
- Python 3.10 or newer.

## Development

```sh
./test
```

`./test` first prints a readable demo, then runs the full test suite and ends with `ALL TESTS PASSED`:

- the demo runs every command through the real hook code on fixed fake Claude Code and Codex sessions;
- your real clipboard is blocked throughout.

It does not show what a real client does; that is checked separately before a release.

```sh
./test --examples                      # also regenerate docs/examples.md from the same run
python3 -m unittest discover -s tests  # the tests alone
```

`docs/examples.md` is generated; a test fails if it no longer matches the tool.

MIT licensed.
