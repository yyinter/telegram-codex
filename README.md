# Telegram Codex Bridge

Remote-control Codex CLI from a Telegram bot. Text messages are forwarded to a
Codex session, Codex's final response is posted back to Telegram, and Telegram
photos/documents are downloaded as local attachments.

There are two implementations:

- `telegram_codex_remote.py` is recommended. It uses Codex's `app-server`
  remote-control protocol, keeps one Codex server process alive, and drives
  persistent threads with `thread/start`, `thread/resume`, and `turn/start`.
- `telegram_codex_bridge.py` is the simpler fallback. It shells out to
  `codex exec resume --last` for each Telegram message.

## What It Supports

- Telegram text -> Codex `app-server` `turn/start`
- Telegram replies -> forwarded as reply context
- Telegram photos -> downloaded locally and passed as `localImage`
- Telegram PDFs/documents -> downloaded locally and passed as file paths
- Codex response -> Telegram text reply
- Codex command/file approvals -> Telegram `/approve <id>` or `/deny <id>`
- Multiple Codex conversations per project with Telegram session slots
- `/sendfile <path>` -> send a local image, PDF, or other file back to Telegram
- `/sessions`, `/new`, `/use`, `/bind`, `/resume`, `/cancel`, `/status`, `/where`, `/help`

## Setup

Create a Telegram bot with BotFather and export its token:

```bash
export TELEGRAM_BOT_TOKEN='123456:bot-token'
```

Run the bridge from the directory Codex should work in:

```bash
python3 telegram_codex_remote.py
```

## Shared TUI + Telegram Mode

If you want Codex TUI and Telegram to control the same live session at the same
time, both clients must connect to the same app-server process. Do not start one
normal `codex` TUI and a separate `telegram_codex_remote.py` stdio app-server
against the same thread id.

Start a shared app-server:

```bash
codex app-server --listen ws://127.0.0.1:8765
```

In another terminal, start the TUI against that server:

```bash
codex --remote ws://127.0.0.1:8765
```

In another terminal, start the Telegram bridge against the same server:

```bash
export TELEGRAM_BOT_TOKEN='123456:bot-token'
export CODEX_APP_SERVER_URL='ws://127.0.0.1:8765'
python3 /data07/home/yaoxinzhi.erii/telegram-codex/telegram_codex_remote.py
```

In this mode, the TUI and Telegram are two clients of one Codex app-server,
which is the architecture needed for Claude-style simultaneous control.

The first run prints a pairing command like:

```text
/pair a1b2c3
```

Send that command to your bot from the Telegram chat you want to authorize. The
paired chat id is saved in `.telegram-codex/remote-state.json`.

If you already know the chat id, skip pairing:

```bash
export TELEGRAM_ALLOWED_CHAT_ID='123456789'
python3 telegram_codex_remote.py
```

## Common Configuration

```bash
# Codex binary. Auto-detects ../node_modules/.bin/codex or PATH by default.
export CODEX_BIN='/path/to/codex'

# Codex workspace. Defaults to the current directory.
export CODEX_WORKDIR='/path/to/project'

# Extra app-server arguments. Defaults to empty.
export CODEX_APP_SERVER_ARGS='--enable realtime_conversation'

# Connect to an existing shared app-server instead of spawning a private stdio app-server.
export CODEX_APP_SERVER_URL='ws://127.0.0.1:8765'

# Codex thread settings for app-server. Defaults shown here.
export CODEX_APPROVAL_POLICY='on-request'
export CODEX_SANDBOX='workspace-write'

# Optional model override.
export CODEX_MODEL='gpt-5.5'

# Fallback bridge only: extra codex exec arguments. Defaults to:
export CODEX_ARGS='--skip-git-repo-check --full-auto'

# Store downloads somewhere else.
export TELEGRAM_ATTACHMENTS_DIR='/tmp/telegram-codex-attachments'

# Allow /sendfile to send absolute paths outside CODEX_WORKDIR.
export TELEGRAM_ALLOW_ABSOLUTE_SEND=1
```

## Telegram Commands

```text
/help
/status
/sessions
/sessions here
/new
/new debug-fa
/use debug-fa
/bind 019e1cf7-200d-7f83-ac25-d8519e7123fc debug-fa
/resume
/cancel
/approve 1
/deny 1
/where
/sendfile output/report.pdf here is the report
```

Plain text is sent to Codex. If you reply to a previous Telegram message, that
reply text is included as context. If you send a photo or PDF with a caption, the
caption and local attachment path are included in the Codex prompt.

## What `resume --last` Means

`codex exec resume --last -` is a non-interactive CLI shortcut: start a new
Codex process, load the most recent saved session from disk, read one prompt
from stdin, run until completion, print/write the final answer, then exit. It
preserves conversation history, but it is not a live remote-control connection.

`telegram_codex_remote.py` uses the newer app-server surface instead. It starts
`codex app-server --listen stdio://`, initializes the JSON-RPC connection, keeps
the process alive, stores the Codex `thread_id`, and sends each Telegram message
as a `turn/start` request. This is the closer match to Claude-style Telegram
plugins.

## Session Binding

Codex app-server sessions are identified by a `thread_id`. The remote bridge
stores Telegram session slots in:

```text
.telegram-codex/remote-state.json
```

The state looks conceptually like this:

```json
{
  "active_slot": "debug-fa",
  "session_slots": {
    "default": "019e1cf7-...",
    "debug-fa": "019e1d02-..."
  },
  "session_slot_cwds": {
    "default": "/path/to/project",
    "debug-fa": "/path/to/other/project"
  }
}
```

Commands:

```text
/sessions
```

Shows saved Telegram slots plus recent Codex threads from the shared app-server,
including their `cwd`. Use `/sessions here` to filter to the bridge's current
`CODEX_WORKDIR`.

```text
/new debug-fa
```

Creates or switches to slot `debug-fa`; the next prompt starts a fresh Codex
thread and stores that thread id in the slot. If the slot was already bound to
a directory, the fresh thread uses that slot directory; otherwise it uses
`CODEX_WORKDIR`.

```text
/use debug-fa
```

Switches Telegram to the saved `debug-fa` slot. Future prompts go to that
Codex thread via `thread/resume` + `turn/start`.

```text
/bind <thread_id> debug-fa
```

Binds an existing Codex thread id to a Telegram slot. Use this when you started
a Codex conversation elsewhere and want Telegram to continue that exact session.
The bridge validates the id against the shared app-server's thread list and
stores that thread's `cwd` with the slot.

Prompts are bound to the slot, thread id, and `cwd` that were active when the
Telegram message was accepted. Switching slots while a message is queued will
not move that queued prompt to another session.

## Security Notes

This bridge lets Telegram users run Codex against your machine. Use either
`TELEGRAM_ALLOWED_CHAT_ID` or the built-in pairing flow, and run it only in a
workspace where Codex is allowed to operate. Avoid
`--dangerously-bypass-approvals-and-sandbox` unless the host is isolated.

## Open-Source Alternatives

- [HeyAgent](https://github.com/gergomiklos/heyagent) is the closest complete
  option I found: Telegram control for AI coding agents with support for
  documents and images.
- [CodexClaw](https://github.com/MackDing/CodexClaw) is a heavier Telegram bot
  runtime around Codex with multi-project and permission-oriented workflows.
- [Telecode](https://github.com/GianlucaP106/telecode) is a smaller Telegram
  wrapper concept for running Codex from Telegram.

This repo keeps a minimal local implementation so you can audit it and adapt the
behavior without committing to a larger bot framework.
