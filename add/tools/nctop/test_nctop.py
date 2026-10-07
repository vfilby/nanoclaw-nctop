#!/usr/bin/env python3
"""nctop's own tests: rendering, the file browser's limits, and a pty walk through every view of the real curses UI.

A fixture install in a temp dir: session mailboxes with NanoClaw v2's schemas, a Claude transcript, logs, agent folders,
and a stub `ncl` that answers `<resource> list --json`. (src/nctop.test.ts checks the same tool against real core.)

    python3 tools/nctop/test_nctop.py
"""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import pty
import select
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'nctop')
ROOT = os.path.realpath(tempfile.mkdtemp(prefix='nctop-'))
os.environ['NANOCLAW_DIR'] = ROOT
os.environ['CONTAINER_RUNTIME'] = os.path.join(ROOT, 'no-such-runtime')  # never talk to a real docker

AG_ANDY, AG_HELPER = 'ag-1790530428887-b4dfiu', 'ag-77123795-40e3-450c-8c93-02835a383a8b'
SESS_LIVE, SESS_TASK = 'sess-1790998277796-0corq1', 'sess-1790534155983-q68nid'
CID = '3a850649-9644-4db2-bd9a-18ec380b8f68'


def iso(offset=0):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(time.time() + offset))


def db(path, script, rows=()):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(script)
    for sql, args in rows:
        c.execute(sql, args)
    c.commit()
    c.close()


INBOUND = """
    CREATE TABLE messages_in (id TEXT PRIMARY KEY, seq INTEGER UNIQUE, kind TEXT NOT NULL, timestamp TEXT NOT NULL,
      status TEXT DEFAULT 'pending', process_after TEXT, recurrence TEXT, series_id TEXT, tries INTEGER DEFAULT 0,
      trigger INTEGER NOT NULL DEFAULT 1, platform_id TEXT, channel_type TEXT, thread_id TEXT,
      content TEXT NOT NULL, source_session_id TEXT, on_wake INTEGER NOT NULL DEFAULT 0);
"""
OUTBOUND = """
    CREATE TABLE messages_out (id TEXT PRIMARY KEY, seq INTEGER UNIQUE, in_reply_to TEXT, timestamp TEXT NOT NULL,
      deliver_after TEXT, recurrence TEXT, kind TEXT NOT NULL, platform_id TEXT, channel_type TEXT,
      thread_id TEXT, content TEXT NOT NULL);
    CREATE TABLE session_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE container_state (id INTEGER PRIMARY KEY CHECK (id = 1), current_tool TEXT,
      tool_declared_timeout_ms INTEGER, tool_started_at TEXT, updated_at TEXT NOT NULL);
"""


def session(id, ag, mg, thread, status, container, last, created):
    return {'id': id, 'agent_group_id': ag, 'messaging_group_id': mg, 'thread_id': thread, 'agent_provider': None,
            'status': status, 'container_status': container, 'last_active': last, 'created_at': created}


def build():
    data = os.path.join(ROOT, 'data')
    central = {
        'groups': [{'id': AG_ANDY, 'name': 'Andy', 'folder': 'main', 'agent_provider': None, 'created_at': iso(-86400)},
                   {'id': AG_HELPER, 'name': 'Helper', 'folder': 'team', 'agent_provider': None,
                    'created_at': iso(-86400)}],
        'messaging-groups': [
            {'id': 'mg-dm', 'channel_type': 'slack', 'platform_id': 'slack:D01', 'instance': 'slack', 'name': None,
             'is_group': 0, 'unknown_sender_policy': 'strict', 'denied_at': None, 'created_at': iso()},
            {'id': 'mg-team', 'channel_type': 'slack', 'platform_id': 'slack:C01', 'instance': 'slack',
             'name': 'team', 'is_group': 1, 'unknown_sender_policy': 'strict', 'denied_at': None, 'created_at': iso()}],
        'users': [{'id': 'slack:U01', 'kind': 'slack', 'display_name': 'Sam', 'created_at': iso()}],
        'user-dms': [{'user_id': 'slack:U01', 'channel_type': 'slack', 'messaging_group_id': 'mg-dm',
                      'resolved_at': iso()}],
        'sessions': [
            session(SESS_LIVE, AG_ANDY, 'mg-dm', 'slack:D01:1790998277.511819', 'active', 'running', iso(-30),
                    iso(-3600)),
            session(SESS_TASK, AG_ANDY, None, 'system:tasks:daily-check-in-474c', 'active', 'stopped', iso(-9000),
                    iso(-86400)),
            session('sess-team-1', AG_HELPER, 'mg-team', 'slack:C01:1790993886.1', 'active', 'stopped', iso(-7000),
                    iso(-8000)),
            session('sess-closed-1', AG_HELPER, 'mg-team', None, 'closed', 'stopped', iso(-90000), iso(-99000))],
        'approvals': [],
        'dropped-messages': [{'channel_type': 'slack', 'platform_id': 'slack:C99', 'user_id': None,
                              'sender_name': 'Pat', 'reason': 'no_agent_wired', 'messaging_group_id': None,
                              'agent_group_id': None, 'message_count': 2, 'first_seen': iso(-600),
                              'last_seen': iso(-300)}],
        'tasks': [
            {'agent_group_id': AG_ANDY, 'session_id': SESS_TASK, 'series_id': 'daily-check-in-474c',
             'row_id': 'task-1', 'status': 'pending', 'process_after': iso(3600), 'recurrence': '0 10 * * *',
             'prompt': 'Daily check-in.\nMore.', 'schedule': '0 10 * * *', 'runs': 6, 'failed_runs': 1,
             'last_run': iso(-82800), 'next_run': iso(3600), 'log': 'tasks/daily-check-in-474c.md'},
            {'agent_group_id': AG_HELPER, 'series_id': 'old-one', 'row_id': 'task-2', 'status': 'completed',
             'prompt': 'done already', 'runs': 1}],
    }
    os.makedirs(os.path.join(ROOT, 'bin'), exist_ok=True)
    with open(os.path.join(ROOT, 'central.json'), 'w') as f:
        json.dump(central, f)
    with open(os.path.join(ROOT, 'bin', 'ncl'), 'w') as f:
        f.write(f'#!{sys.executable}\nimport json, sys\n'
                f'd = json.load(open({os.path.join(ROOT, "central.json")!r}))\n'
                'assert sys.argv[2:3] == ["list"] and sys.argv[-1] == "--json", sys.argv\n'
                'print(json.dumps({"id": "x", "ok": True, "data": d.get(sys.argv[1], [])}, indent=2))\n')
    os.chmod(os.path.join(ROOT, 'bin', 'ncl'), 0o755)

    sdir = os.path.join(data, 'v2-sessions', AG_ANDY, SESS_LIVE)
    db(os.path.join(sdir, 'inbound.db'), INBOUND, [
        ('insert into messages_in (id, seq, kind, timestamp, status, content) values (?,?,?,?,?,?)',
         ('i1', 2, 'chat-sdk', iso(-300), 'completed', json.dumps(
             {'_type': 'chat:Message', 'text': 'Can the build server reach the registry?', 'sender': 'Sam',
              'senderName': 'Sam', 'author': {'fullName': 'Sam'}, 'attachments': []}))),
        ('insert into messages_in (id, seq, kind, timestamp, status, content) values (?,?,?,?,?,?)',
         ('i2', 4, 'system', iso(-200), 'completed', json.dumps(
             {'type': 'ops_response', 'requestId': 'ops-1', 'status': 'ok',
              'result': {'output': 'line one\nline two\nline three\nline four'}}))),
        ('insert into messages_in (id, seq, kind, timestamp, status, tries, content) values (?,?,?,?,?,?,?)',
         ('i3', 6, 'chat', iso(-100), 'failed', 3, json.dumps({'text': 'retry me', 'sender': 'system'}))),
    ])
    db(os.path.join(data, 'v2-sessions', AG_HELPER, 'sess-team-1', 'inbound.db'), INBOUND, [
        ('insert into messages_in (id, seq, kind, timestamp, status, content) values (?,?,?,?,?,?)',
         ('t1', 2, 'chat-sdk', iso(-8000), 'completed',
          json.dumps({'text': 'Book these for the offsite:\nBooking 123\nPassword: 4520', 'sender': 'Sam'})))])
    db(os.path.join(sdir, 'outbound.db'), OUTBOUND, [
        ('insert into messages_out (id, seq, kind, timestamp, content) values (?,?,?,?,?)',
         ('o1', 1, 'system', iso(-250), json.dumps({'action': 'set_status', 'text': 'Reading compose.yaml…'}))),
        ('insert into messages_out (id, seq, kind, timestamp, content) values (?,?,?,?,?)',
         ('o2', 3, 'system', iso(-240), json.dumps(
             {'action': 'send_email', 'requestId': 'mail-1', 'to': ['x@example.com'], 'subject': 'Hello',
              'body': 'SECRET BODY'}))),
        ('insert into messages_out (id, seq, kind, timestamp, content) values (?,?,?,?,?)',
         ('o3', 5, 'chat', iso(-60), json.dumps({'text': 'Yes: the registry answers from the build server.'}))),
        ('insert into session_state values (?,?,?)', ('continuation:claude', CID, iso())),
        ('insert into container_state values (?,?,?,?,?)', (1, 'mcp__nanoclaw__ops_run', 60000, iso(-12), iso())),
    ])
    open(os.path.join(sdir, '.heartbeat'), 'w').close()

    proj = os.path.join(data, 'v2-sessions', AG_ANDY, '.claude-shared', 'projects', '-workspace-agent')
    os.makedirs(proj, exist_ok=True)
    records = [
        {'type': 'queue-operation', 'operation': 'enqueue', 'content': 'x'},
        {'type': 'user', 'timestamp': iso(-300), 'message': {'role': 'user', 'content':
            '<context timezone="UTC" />\n' + '\n'.join(f'<dm-history>old {i}</dm-history>' for i in range(20)) +
            '\n<message sender="Sam">check the docs</message>'}},
        {'type': 'assistant', 'timestamp': iso(-290), 'message': {'content': [
            {'type': 'thinking', 'thinking': 'secret thoughts'},
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'curl -sI https://registry',
                                                                     'description': 'Check the registry'}}]}},
        {'type': 'user', 'timestamp': iso(-285), 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'r1\nr2\nr3\nr4'}]}},
        {'type': 'assistant', 'timestamp': iso(-280), 'message': {'content': [
            {'type': 'tool_use', 'id': 't2', 'name': 'mcp__nanoclaw__ops_run', 'input': {'host': 'b', 'tier': 'rw'}}]}},
        {'type': 'user', 'timestamp': iso(-279), 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 't2', 'is_error': True, 'content': [{'type': 'text', 'text': 'denied'}]}]}},
        {'type': 'attachment', 'timestamp': iso(-270), 'attachment': {'type': 'hook_success', 'hookName': 'Stop',
                                                                     'content': 'hook out'}},
        {'type': 'attachment', 'timestamp': iso(-265), 'attachment': {'type': 'queued_command', 'prompt': 'done'}},
        {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': iso(-262)},
        {'type': 'assistant', 'timestamp': iso(-260), 'isSidechain': True, 'message': {'content': [
            {'type': 'text', 'text': 'subagent says hi'}]}},
        {'type': 'assistant', 'timestamp': iso(-250), 'message': {'content': [
            {'type': 'text', 'text': 'Checked: the registry answers.'}]}},
        {'type': 'ai-title', 'aiTitle': 'Build server registry reachability'},
    ]
    with open(os.path.join(proj, CID + '.jsonl'), 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')
        f.write('{"type": "user", "partial')  # a line still being written

    groups = os.path.join(ROOT, 'groups')
    for folder in ('main', 'team'):
        os.makedirs(os.path.join(groups, folder, 'memory'), exist_ok=True)
        with open(os.path.join(groups, folder, 'CLAUDE.md'), 'w') as f:
            f.write(f'# {folder}\n\nPersona.\n')
        with open(os.path.join(groups, folder, 'memory', 'index.md'), 'w') as f:
            f.write('# Memory\n- likes tea\n')
    os.makedirs(os.path.join(groups, 'main', 'tasks'), exist_ok=True)
    with open(os.path.join(groups, 'main', 'tasks', 'daily-check-in-474c.md'), 'w') as f:
        f.write('- 2026-10-02 10:00 ran ok\n')
    os.makedirs(os.path.join(groups, 'main', '.ssh'), exist_ok=True)
    open(os.path.join(groups, 'main', 'id_ed25519'), 'w').close()
    with open(os.path.join(groups, 'main', 'github-token'), 'w') as f:
        f.write('ghp_shouldneverbeshown\n')
    with open(os.path.join(groups, 'main', 'container.json'), 'w') as f:
        f.write('{"packages":{"apt":["jq"]}}')
    os.symlink('/etc', os.path.join(groups, 'team', 'escape'))

    logs = os.path.join(ROOT, 'logs')
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, 'nanoclaw.log'), 'w') as f:
        f.write('[21:36:37.906] \x1b[32mINFO\x1b[39m \x1b[36mPruned session-echo backlog\x1b[39m sessionId="s"\n')
        f.write('[21:36:38.001] \x1b[33mWARN\x1b[39m slow poll\n')
        f.write('[21:36:39.002] \x1b[31mERROR\x1b[39m delivery failed\n')
    with open(os.path.join(logs, 'nanoclaw.error.log'), 'w') as f:
        f.write('Error: boom\n')


build()
loader = importlib.machinery.SourceFileLoader('nctop', SCRIPT)
spec = importlib.util.spec_from_loader('nctop', loader)
nt = importlib.util.module_from_spec(spec)
loader.exec_module(nt)
HOST = nt.Host()
HOST.refresh()


def texts(lines):
    return [nt.plain(segs) for segs, _ in lines]


def dump(*args):
    p = subprocess.run([sys.executable, SCRIPT, '--dump', *args], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


class Unit(unittest.TestCase):
    def test_paths_follow_the_root(self):
        self.assertEqual(nt.ROOT, ROOT)
        self.assertEqual(nt.NCL, os.path.join(ROOT, 'bin', 'ncl'))
        self.assertRegex(nt.SLUG, r'^[0-9a-f]{8}$')
        self.assertEqual(nt.SYSTEMD_UNIT, f'nanoclaw-v2-{nt.SLUG}.service')
        self.assertEqual(nt.CONTAINER_PREFIX, f'ncl-{nt.SLUG}-')

    def test_wrap_respects_width_and_indent(self):
        rows = nt.wrap([('abcdefghij', None), ('KLMNO', 'bad')], 6, indent=2)
        self.assertEqual([nt.plain(r) for r in rows], ['abcdef', '  ghij', '  KLMN', '  O'])
        self.assertTrue(all(nt.width(nt.plain(r)) <= 6 for r in rows))
        self.assertEqual(nt.width('日本'), 4)
        self.assertEqual(nt.width(nt.trunc('日本語のテキスト', 7)), 7)

    def test_clean_strips_ansi_and_controls(self):
        self.assertEqual(nt.clean('\x1b[32mINFO\x1b[39m a\tb\x07'), 'INFO a    b')

    def test_ncl_data(self):
        self.assertEqual([g['name'] for g in HOST.groups().values()], ['Andy', 'Helper'])
        self.assertEqual(HOST.dm_names(), {'mg-dm': 'Sam'})
        self.assertEqual(HOST.sessions()[0]['id'], SESS_LIVE)  # newest first
        self.assertEqual(len(HOST.dropped()), 1)
        self.assertEqual(len(HOST.tasks), 2)

    def test_where_labels(self):
        mgs, dms = HOST.messaging_groups(), HOST.dm_names()
        by_id = {s['id']: s for s in HOST.sessions()}
        self.assertEqual(nt.where(by_id[SESS_LIVE], mgs, dms), 'DM Sam')
        self.assertEqual(nt.where(by_id['sess-team-1'], mgs, dms), '#team')
        self.assertEqual(nt.where(by_id[SESS_TASK], mgs, dms), 'task daily-check-in-474c')

    def test_session_rows(self):
        HOST.containers = [{'Names': nt.CONTAINER_PREFIX + SESS_LIVE, 'Status': 'Up 1 hour'},
                           {'Names': 'ncl-otherslug-' + SESS_LIVE, 'Status': 'Up 1 hour'}]
        rows = {r['s']['id']: r for r in nt.session_rows(HOST)}
        self.assertNotIn('sess-closed-1', rows)
        live = rows[SESS_LIVE]
        self.assertEqual(live['title'], 'Build server registry reachability')
        self.assertEqual(nt.container_cell(live), ('● run', 'ok'))
        self.assertTrue(nt.tool_cell(live).startswith('ops_run '))
        self.assertEqual(rows[SESS_TASK]['title'], '')
        self.assertEqual(rows['sess-team-1']['title'], 'Book these for the offsite:')  # first line only
        self.assertIn('sess-closed-1', {r['s']['id'] for r in nt.session_rows(HOST, include_closed=True)})
        HOST.containers = [{'Names': 'ncl-otherslug-' + SESS_LIVE, 'Status': 'Up 1 hour'}]  # another install's
        self.assertEqual(nt.container_cell(nt.session_rows(HOST)[0]), ('? run', 'warn'))
        HOST.containers = None

    def test_transcript_rendering(self):
        path = nt.transcript_path({'agent_group_id': AG_ANDY, 'id': SESS_LIVE}, {'continuation:claude': CID})
        t = nt.Tail(path, nt.MAX_TRANSCRIPT)
        lines, _ = t.read()
        self.assertEqual(len(lines), 12)  # the partial last line waits
        joined = '\n'.join(x for ln in lines for x in texts(nt.transcript_lines(json.loads(ln))))
        self.assertIn('⚙ Bash curl -sI https://registry  (Check the registry)', joined)
        self.assertIn('⚙ ops_run', joined)
        self.assertIn('⎿ denied', joined)
        self.assertIn('… +2 lines', joined)
        self.assertIn('check the docs', joined)
        self.assertIn('earlier lines', joined)
        self.assertIn('» (while busy) done', joined)
        self.assertIn('conversation compacted', joined)
        self.assertIn('⤷ ● subagent says hi', joined)
        self.assertNotIn('secret thoughts', joined)
        self.assertNotIn('hook out', joined)
        self.assertNotIn('title:', joined)
        verbose = '\n'.join(x for ln in lines for x in texts(nt.transcript_lines(json.loads(ln), verbose=True)))
        self.assertIn('secret thoughts', verbose)
        self.assertIn('old 0', verbose)
        with open(path, 'a') as f:
            f.write('"}\n')
        more, _ = t.read()
        self.assertEqual(len(more), 1)

    def test_messages_rendering(self):
        s = {'agent_group_id': AG_ANDY, 'id': SESS_LIVE}
        out = '\n'.join(x for d, r in nt.session_messages(s) for x in texts(nt.message_lines(d, r)))
        self.assertIn('» Sam: Can the build server reach the registry?', out)
        self.assertIn('⎿ ops_response ok: line one', out)
        self.assertIn('… 1 more lines', out)
        self.assertIn('[failed] » system: retry me', out)
        self.assertIn('⚙ send_email to x@example.com: "Hello"', out)
        self.assertNotIn('SECRET BODY', out)
        self.assertIn('… Reading compose.yaml…', out)
        self.assertIn('● Yes: the registry answers', out)
        self.assertLess(out.index('Can the build'), out.index('Yes: the registry'))

    def test_files_hide_secrets_and_stay_in_root(self):
        entries, err = nt.list_dir(os.path.join(nt.GROUPS_DIR, 'main'))
        names = [e['name'] for e in entries]
        self.assertIsNone(err)
        for secret in ('github-token', '.ssh', 'id_ed25519'):
            self.assertNotIn(secret, names)
        self.assertEqual(names[:3], ['memory', 'tasks', 'CLAUDE.md'])
        self.assertIn('hidden', texts(nt.file_lines(os.path.join(nt.GROUPS_DIR, 'main', 'github-token')))[0])
        v = nt.FilesView(HOST)
        self.assertFalse(v.go(os.path.join(nt.GROUPS_DIR, 'team', 'escape')))
        self.assertFalse(v.go(ROOT))
        self.assertTrue(v.go(os.path.join(nt.GROUPS_DIR, 'team')))
        self.assertIn('"apt": [', '\n'.join(texts(nt.file_lines(os.path.join(nt.GROUPS_DIR, 'main', 'container.json')))))

    def test_pager_follow_and_search(self):
        p = nt.Pager(follow=True)
        p.set([nt.L(f'line {i}') for i in range(100)])

        class Win:
            def getmaxyx(self):
                return (10, 40)

            def addstr(self, *a):
                pass
        p.draw(Win(), 0, 10, 40)
        self.assertEqual(p.top, 90)
        p.key(nt.curses.KEY_UP)
        self.assertFalse(p.follow)
        p.draw(Win(), 0, 10, 40)
        self.assertEqual(p.top, 89)
        p.key(nt.curses.KEY_DOWN)
        p.draw(Win(), 0, 10, 40)
        self.assertTrue(p.follow)  # back at the bottom: following again
        p.top = 0
        self.assertTrue(p.find('line 42'))
        self.assertEqual(p.top, 42)


class Dump(unittest.TestCase):
    def test_overview(self):
        out = dump('overview')
        self.assertIn('Agents', out)
        self.assertIn('Andy', out)
        self.assertIn('dropped 2 from Pat on slack:C99: no_agent_wired', out)
        self.assertIn('inbound chat failed tries=3', out)
        self.assertIn('daily-check-in-474c', out)
        self.assertIn('Error: boom', out)

    def test_sessions_tasks_logs_files_paths(self):
        self.assertIn('Build server registry reachability', dump('sessions'))
        tasks = dump('tasks')
        self.assertIn('daily-check-in-474c', tasks)
        self.assertNotIn('old-one', tasks)
        self.assertIn('old-one', dump('tasks', '--all'))
        logs = dump('logs')
        self.assertIn('ERROR delivery failed', logs)
        self.assertNotIn('\x1b', logs)
        self.assertIn('main/', dump('files'))
        self.assertIn('tasks/', dump('files', os.path.join(nt.GROUPS_DIR, 'main')))
        self.assertIn(f'nanoclaw-v2-{nt.SLUG}.service', dump('paths'))

    def test_session(self):
        out = dump('session', SESS_LIVE)
        self.assertIn('Checked: the registry answers.', out)
        self.assertIn(' transcript ', out)
        self.assertIn('● Yes: the registry answers', dump('session', SESS_LIVE, '--messages'))
        self.assertIn('no messages', dump('session', SESS_TASK, '--verbose'))  # no transcript: messages mode


class Tui(unittest.TestCase):
    """Drives the real curses UI in a pseudo-terminal through every view."""

    def run_keys(self, keys, rows=40, cols=140):
        pid, fd = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            os.execv(sys.executable, [sys.executable, SCRIPT])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        out = b''

        def drain(secs):
            nonlocal out
            end = time.time() + secs
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    try:
                        out += os.read(fd, 65536)
                    except OSError:
                        return
        drain(2.0)
        for k in keys:
            try:
                os.write(fd, k.encode() if isinstance(k, str) else k)
            except OSError:
                break  # it exited early; the assertions say why
            drain(0.4)
        drain(0.5)
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), out.decode('utf-8', 'replace')

    def test_walk_every_view(self):
        keys = ['2', 'j', 'k', '\r', 'm', 'v', 't', 'G', 'k', 'f', '/', 'docs\r', 'n', 'o', '\r', 'q', 'q',
                'c', 'c', '3', 'a', '\r', 's', 'q', 'q', '4', 'e', 'w', '5', 'j', '\r', 'h', 's', '?', 'q',
                '\t', '\x1b[Z', '1', 'r', 'G', 'g', 'q']
        code, out = self.run_keys(keys)
        self.assertNotIn('Traceback', out)
        self.assertEqual(code, 0, out[-2000:])
        for word in ('Overview', 'Sessions', 'Build server registry reachability', 'daily-check-in', 'nanoclaw.log'):
            self.assertIn(word, out)

    def test_tiny_terminal(self):
        code, out = self.run_keys(['q'], rows=4, cols=20)
        self.assertEqual(code, 0, out[-2000:])


if __name__ == '__main__':
    unittest.main(verbosity=2)
