# nctop: a terminal dashboard for NanoClaw

`nctop` is a `top` for a [NanoClaw](https://github.com/nanocoai/nanoclaw) v2 install: service and host health,
agents, sessions followed live (the agent's Claude transcript with its tool calls, or its messages), scheduled tasks,
the host log, and the agent folders. It runs in any terminal, over SSH.

- Read-only: it never writes to NanoClaw's databases or files, and has no network listener.
- One Python 3 standard-library file. NanoClaw's source is untouched.
- Ships a vitest test that runs nctop against a test install built from core's own migrations and CLI, so it fails
  when NanoClaw changes something nctop reads.

This repository is the NanoClaw skill `/add-nctop`. It is not part of upstream NanoClaw.

## Install

From the root of your NanoClaw checkout:

```bash
git clone https://github.com/vfilby/nanoclaw-nctop.git .claude/skills/add-nctop
```

Then run `/add-nctop` in Claude Code there. It copies the tool to `tools/nctop/`, runs the tests, and tells you how to
start it (`tools/nctop/nctop`). [SKILL.md](SKILL.md) has the details, keys and troubleshooting.

Without Claude Code, do what SKILL.md's "Apply" section says by hand: copy `add/tools/nctop/*` to `tools/nctop/` and
`add/src/nctop.test.ts` to `src/`, `chmod +x tools/nctop/nctop`, then run the two test commands.

## Update

```bash
git -C .claude/skills/add-nctop pull
```

and run `/add-nctop` again (applying is safe to repeat).

## Remove

See [REMOVE.md](REMOVE.md), then `rm -rf .claude/skills/add-nctop`.

## Requirements

- NanoClaw v2 (tested against upstream `main`; CI re-checks weekly).
- Python 3.9+ with `curses` (`/usr/bin/python3` on macOS, `python3` on Debian/Ubuntu).
- The docker CLI, optionally, for container state.

## License

MIT, like NanoClaw.
