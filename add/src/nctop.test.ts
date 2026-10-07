/**
 * nctop against real core: a test install built with core's own migrations, DB helpers, session mailboxes and CLI
 * dispatcher, read by the real tools/nctop/nctop.
 *
 * nctop reads core without importing it (it is a Python tool), so this is where core drift shows up. Red when:
 * - an `ncl <resource> list --json` call nctop makes stops dispatching, or loses the fields nctop shows;
 * - a session mailbox table or column nctop reads changes (rows are written with core's own helpers);
 * - the install slug or the agent container name derivation changes (compared with core's functions);
 * - the transcript lookup moves: the agent-runner's continuation key, or the `.claude-shared` folder.
 */
import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import Database from 'better-sqlite3';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('./config.js', async () => {
  const actual = await vi.importActual<typeof import('./config.js')>('./config.js');
  return { ...actual, DATA_DIR: '/tmp/nanoclaw-test-nctop/data', GROUPS_DIR: '/tmp/nanoclaw-test-nctop/groups' };
});

vi.mock('./container-runner.js', () => ({
  wakeContainer: vi.fn().mockResolvedValue(undefined),
  isContainerRunning: vi.fn().mockReturnValue(false),
  getActiveContainerCount: vi.fn().mockReturnValue(0),
  killContainer: vi.fn(),
  buildAgentGroupImage: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('./container-restart.js', () => ({
  restartAgentGroupContainers: vi.fn().mockResolvedValue(0),
}));

import { dispatch } from './cli/dispatch.js';
import { parseArgv } from './cli/parse-argv.js';
import './cli/resources/index.js';
import { closeDb, createAgentGroup, createMessagingGroup, initTestDb, runMigrations } from './db/index.js';
import { createSession } from './db/sessions.js';
import { agentContainerName } from './drivers/docker-driver.js';
import { getInstallSlug } from './install-slug.js';
import { inboundDbPath, outboundDbPath } from './mailbox/sqlite/paths.js';
import { ensureSchema, insertMessage } from './mailbox/sqlite/session-db.js';
import { initSessionFolder, writeOutboundDirect } from './session-manager.js';
import type { MessagingGroup } from './types.js';

const ROOT = '/tmp/nanoclaw-test-nctop';
const NCTOP = path.resolve('tools/nctop/nctop');
const AG = 'ag-nctop-test';
const SESS = 'sess-1790000000000-nctop1';
const CID = '00000000-0000-4000-8000-00000000c1d0';
const LONG_SESS = `sess-${'x'.repeat(60)}`;

function nctop(...args: string[]): string {
  return execFileSync('python3', [NCTOP, '--dump', ...args], {
    encoding: 'utf-8',
    env: { ...process.env, NANOCLAW_DIR: ROOT, CONTAINER_RUNTIME: path.join(ROOT, 'no-such-runtime') },
    timeout: 60_000,
  });
}

beforeAll(async () => {
  await import('./mailbox/compose.js'); // registers the session mailbox (test-setup does it per test, after this)
  fs.rmSync(ROOT, { recursive: true, force: true });
  for (const dir of ['bin', 'logs', 'ncl', 'groups/main/memory'])
    fs.mkdirSync(path.join(ROOT, dir), { recursive: true });
  fs.writeFileSync(path.join(ROOT, 'groups/main/CLAUDE.md'), '# main\n');

  const db = await initTestDb();
  await runMigrations(db);
  const now = new Date().toISOString();
  await createAgentGroup({ id: AG, name: 'Andy', folder: 'main', agent_provider: null, created_at: now });
  await createMessagingGroup({
    id: 'mg-nctop-test',
    channel_type: 'slack',
    platform_id: 'slack:C0NCTOP',
    name: 'nctop-room',
    is_group: 1,
    unknown_sender_policy: 'strict',
    created_at: now,
  } as MessagingGroup);
  await createSession({
    id: SESS,
    agent_group_id: AG,
    messaging_group_id: 'mg-nctop-test',
    thread_id: 'slack:C0NCTOP:1790000000.000100',
    agent_provider: null,
    status: 'active',
    container_status: 'stopped',
    last_active: now,
    created_at: now,
  });

  initSessionFolder(AG, SESS);
  ensureSchema(inboundDbPath(AG, SESS), 'inbound');
  ensureSchema(outboundDbPath(AG, SESS), 'outbound');
  const inbound = new Database(inboundDbPath(AG, SESS));
  insertMessage(inbound, {
    id: 'in-1',
    kind: 'chat',
    timestamp: now,
    platformId: 'slack:C0NCTOP',
    channelType: 'slack',
    threadId: null,
    content: JSON.stringify({ text: 'ping from the test', sender: 'Sam' }),
    processAfter: null,
    recurrence: null,
  });
  inbound.close();
  await writeOutboundDirect(AG, SESS, {
    id: 'out-1',
    kind: 'chat',
    platformId: 'slack:C0NCTOP',
    channelType: 'slack',
    threadId: null,
    content: JSON.stringify({ text: 'pong from the agent' }),
  });

  // The agent-runner stores its Claude session id under continuation:<provider>; Claude Code keeps the transcript in
  // the group's .claude-shared (the container's ~/.claude).
  const outbound = new Database(outboundDbPath(AG, SESS));
  outbound
    .prepare('INSERT INTO session_state (key, value, updated_at) VALUES (?, ?, ?)')
    .run('continuation:claude', CID, now);
  outbound.close();
  const projects = path.join(ROOT, 'data/v2-sessions', AG, '.claude-shared/projects/-workspace-agent');
  fs.mkdirSync(projects, { recursive: true });
  fs.writeFileSync(
    path.join(projects, `${CID}.jsonl`),
    JSON.stringify({
      type: 'assistant',
      timestamp: now,
      message: { content: [{ type: 'text', text: 'hello from the transcript' }] },
    }) + '\n',
  );

  // Every ncl call nctop makes (it lists them), answered by core's real dispatcher, parsed the way bin/ncl parses.
  const calls = nctop('paths')
    .split('\n')
    .filter((l) => l.startsWith('ncl call'))
    .map((l) => l.replace(/^ncl call\s+/, '').split(' '));
  expect(calls.length).toBeGreaterThan(5);
  for (const argv of calls) {
    const { command, args } = parseArgv(argv);
    const frame = await dispatch({ id: `nctop-${command}`, command, args }, { caller: 'host' });
    expect(frame, `ncl ${argv.join(' ')}`).toMatchObject({ ok: true });
    fs.writeFileSync(path.join(ROOT, 'ncl', `${argv[0]}.json`), JSON.stringify(frame));
  }
  fs.writeFileSync(path.join(ROOT, 'bin/ncl'), '#!/bin/sh\nexec cat "$(dirname "$0")/../ncl/$1.json"\n', {
    mode: 0o755,
  });
});

afterAll(async () => {
  await closeDb();
  fs.rmSync(ROOT, { recursive: true, force: true });
});

describe('nctop against core', () => {
  it('derives the install slug and container names as core does', () => {
    const slug = getInstallSlug(ROOT);
    const out = nctop('paths', SESS, LONG_SESS);
    expect(out).toContain(`slug            ${slug}`);
    expect(out).toContain(`nanoclaw-v2-${slug}.service`);
    for (const sessionId of [SESS, LONG_SESS]) {
      const name = agentContainerName({ key: { installSlug: slug, agentGroupId: AG, sessionId } });
      expect(out).toContain(`container       ${sessionId} ${name}`);
    }
  });

  it('lists the session with its agent, channel and opening message', () => {
    const out = nctop('sessions');
    expect(out).toMatch(/Andy \| #nctop-room \| .* \| ping from the test/);
  });

  it("reads the session's messages from its mailbox", () => {
    const out = nctop('session', SESS, '--messages');
    expect(out).toContain('» Sam: ping from the test');
    expect(out).toContain('● pong from the agent');
  });

  it("finds the session's Claude transcript", () => {
    expect(nctop('session', SESS)).toContain('● hello from the transcript');
  });

  it('looks the transcript up under the key the agent-runner stores it with', () => {
    const src = fs.readFileSync('container/agent-runner/src/db/session-state.ts', 'utf-8');
    expect(src).toContain('`continuation:${providerName.toLowerCase()}`');
  });

  it('shows the agent on the overview', () => {
    const out = nctop('overview');
    expect(out).toMatch(/Andy\s+main\s+0 running\s+1 sessions/);
    expect(out).not.toMatch(/ncl \S+ list:/); // no ncl failures among the problems
  });
});
