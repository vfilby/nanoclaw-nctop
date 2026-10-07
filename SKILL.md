---
name: add-nctop
description: Add nctop, a read-only terminal dashboard for a NanoClaw install - service and host health, agents, sessions followed live (the agent's own transcript with its tool calls, or its messages), scheduled tasks, the host log, and the agent folders. One Python 3 standard-library file; NanoClaw's source is untouched.
---

# /add-nctop: terminal dashboard

`nctop` is a `top` for NanoClaw: one screen to see what the install is doing, in any terminal, over SSH.

| Tab | Shows |
|-----|-------|
| 1 Overview | Service state (systemd or launchd), host load/memory/disk, other containers, each agent's sessions and last reply, running sessions with the tool in use and container CPU/memory, what needs attention (pending approvals, dropped messages, failed inbound messages, the error log's tail), the next scheduled tasks |
| 2 Sessions | Every active session: agent, channel, container, current tool, last activity, title. Enter follows one live: `t` the agent's Claude transcript (text, tool calls, results, sub-agents, compactions), `m` the session's inbound and outbound messages, `v` everything (thinking, hooks, full bodies) |
| 3 Tasks | Schedule, next and last run, failures; Enter shows the task and its run log |
| 4 Logs | `logs/nanoclaw.log` followed, or the error log; warnings-only filter |
| 5 Files | `groups/`: each agent's `CLAUDE.md`, skills, memory and task logs, sortable by modification time |

Press `?` in it for every key.

How it reads:

- Central data (groups, sessions, messaging groups, users, DMs, approvals, dropped messages, tasks) comes from
  `ncl <resource> list --json`, in parallel, every 5 seconds (tasks and dropped messages every 30).
- Messages and container state come from the session mailboxes (`data/v2-sessions/<group>/<session>/inbound.db`,
  `outbound.db`), which `ncl` has no resource for. They are opened read-only.
- The transcript is the Claude Code session the agent-runner recorded under `continuation:claude` in the
  session's `outbound.db`, read from the group's `.claude-shared/projects/`.
- Service state comes from `systemctl --user show` or `launchctl list`. Containers come from `docker ps` and
  `docker stats` (or `$CONTAINER_RUNTIME`).

Nothing in it writes or changes anything. It has no network listener and no auth of its own: it shows what the user
running it can read, so reach it the way you reach the host (SSH).

The Files tab never lists or opens anything that looks like a secret: `.ssh`, `.gnupg`, `.env`, `.netrc`, `.aws`,
`.docker`, `id_*`, `*.pem`, `*.key`, `*.p12`, `*.kdbx`, and names containing `token`, `secret`, `credential` or
`passw`. It never follows a symlink out of its root. A session whose transcript has no title shows the first line of
its first message.

## Requirements

- Python 3.9 or newer with `curses`: `/usr/bin/python3` on macOS, the `python3` package on Debian/Ubuntu.
- The docker CLI for container state (optional: without it the container columns stay empty).

Check:

```nc:run effect:check
python3 -c 'import curses, sqlite3, sys; assert sys.version_info >= (3, 9)'
```

## Apply

### 1. Copy the tool and its tests

```nc:copy
add/tools/nctop/nctop -> tools/nctop/nctop
add/tools/nctop/test_nctop.py -> tools/nctop/test_nctop.py
add/src/nctop.test.ts -> src/nctop.test.ts
```

Then make it executable:

```nc:run effect:build
chmod +x tools/nctop/nctop
```

`src/nctop.test.ts` builds a test install with core's own migrations, DB helpers, session mailboxes and CLI
dispatcher, then runs the real `nctop --dump` against it. It fails when core changes anything nctop reads:
- an `ncl` resource or field;
- a mailbox table or column;
- the install slug;
- the container name derivation;
- the transcript key.

`tools/nctop/test_nctop.py` covers nctop's own rendering and file-browser limits against a fixture, and drives the
curses UI in a pseudo-terminal through every view.

### 2. Test

```nc:run effect:test
pnpm exec vitest run src/nctop.test.ts
```

```nc:run effect:test
python3 tools/nctop/test_nctop.py
```

### 3. Run it

From the project root, as the user the install runs as:

```bash
tools/nctop/nctop
```

On a host where NanoClaw runs under a dedicated service account, run it as that account
(`sudo -u <account> /path/to/nanoclaw/tools/nctop/nctop`): transcripts and the mailboxes are private to it. To have it
on `PATH`, link it, as with `ncl`:

```bash
ln -s "$(pwd)/tools/nctop/nctop" ~/.local/bin/nctop
```

`tools/nctop/nctop --dump overview|sessions|tasks|logs|paths|files|session <id>` prints one view as text, without a
terminal (`--dump paths` shows the root, slug, service name and every `ncl` call it makes).

## Troubleshooting

- **"the install belongs to …"** in Problems: it is running as a different user than the install's owner. Run it as
  the owner; otherwise transcripts and mailboxes are unreadable.
- **"no service … for this checkout"**: the service name is derived from the checkout's path (as setup names it). A
  service started from another clone of NanoClaw has a different name. Run nctop from the checkout the service runs.
- **`ncl … list` failures** in Problems: `ncl` talks to the running host over `data/ncl.sock`; with the host stopped,
  the central tables stay empty. Messages, transcripts, logs and files still show.
- **`systemctl --user show: … Failed to connect to user scope bus`** (Linux, under `sudo -u`): nctop sets
  `XDG_RUNTIME_DIR=/run/user/<uid>` when that directory exists; the account needs lingering
  (`loginctl enable-linger <account>`) for it to.
- **Garbled lines or no colours:** set `TERM` (for example `xterm-256color`) and a UTF-8 locale (`LANG=C.UTF-8`).
- **Somewhere else:** `NANOCLAW_DIR` points nctop at another checkout; `NCTOP_NCL` at another `ncl`.

## Removal

See [REMOVE.md](REMOVE.md).
