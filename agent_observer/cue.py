"""Cue decisions and successful ContextMD reads, without serving or prompt text.

The source is a read-only snapshot. Local mirror tables are replaced atomically
on import because pending reads and delivery outcomes can change in place.
Native events are linked, never duplicated into the events table.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from . import privacy

WINDOW_SECONDS = 1800
SERVED = frozenset({'injected', 'truncated', 'unverified', 'awaiting_evidence', 'hidden'})
FULL = frozenset({'read_tool', 'shell_full'})
KINDS = FULL | {'read_partial', 'shell_partial'}
IDENT = re.compile(r'^[A-Za-z0-9_.:/@+-]{1,512}$')
ENTRY_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$')
REQUIRED = {
    'moments': {'id', 'received_at', 'harness', 'session', 'epoch', 'holdout',
                'kind', 'tool_use_id', 'owners_json', 'cue_status', 'delivery'},
    'candidates': {'id', 'moment_id', 'matcher', 'entry', 'version', 'score',
                   'outcome', 'stage', 'served_at'},
    'reads': {'harness', 'session', 'epoch', 'path', 'kind', 'moment_id',
              'read_at', 'pending_tool_use_id'},
    'deliveries': {'delivery', 'stage'},
}


def source_path(source=None):
    return Path(source or Path(os.environ.get('T3CODE_HOME', Path.home() / '.t3'))
                / 'userdata/contextmd.sqlite').expanduser().absolute()


def _open(source):
    con = sqlite3.connect(source_path(source).as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def _schema(con):
    for table, required in REQUIRED.items():
        if not required <= {r[1] for r in con.execute(f'PRAGMA table_info({table})')}:
            return 'schema_drift'
    return None


def source_status(source=None):
    if not source_path(source).is_file():
        return 'missing_database'
    try:
        with closing(_open(source)) as con:
            gap = _schema(con)
            if gap:
                return gap
            return 'ok' if con.execute(
                "SELECT 1 FROM moments WHERE cue_status='ok' LIMIT 1").fetchone() else 'no_cue'
    except (sqlite3.Error, OSError):
        return 'unreadable_database'


def _ensure(con):
    # No global ledger migration or native parser changes are needed.
    con.execute('CREATE TABLE IF NOT EXISTS cue_import(status TEXT, imported_at REAL, watermark REAL)')
    con.execute('''CREATE TABLE IF NOT EXISTS cue_moments(
        id INTEGER PRIMARY KEY, at REAL, harness TEXT, session TEXT, epoch INTEGER,
        holdout INTEGER, kind TEXT, tool_use_id TEXT, cue_status TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS cue_candidates(
        id INTEGER PRIMARY KEY, moment_id INTEGER, entry TEXT, version TEXT,
        score REAL, outcome TEXT, stage TEXT, served_at REAL, delivery_stage TEXT,
        target TEXT, target_status TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS cue_reads(
        harness TEXT, session TEXT, epoch INTEGER, path TEXT, kind TEXT,
        moment_id INTEGER, at REAL, native_event_id INTEGER,
        PRIMARY KEY(harness, session, epoch, path, kind, moment_id, at))''')
    con.execute('CREATE INDEX IF NOT EXISTS cue_read_path ON cue_reads(harness, session, epoch, path, at)')


def _id(value):
    return value if isinstance(value, str) and IDENT.fullmatch(value) else None


def _path(value):
    if (not isinstance(value, str) or not value.startswith('/') or len(value) > 8192
            or any(ord(c) < 32 for c in value)):
        return None
    safe = privacy.filter_target(value)
    return safe if safe == value else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _targets(roots):
    """Resolve only version-matching files, never infer an old target from a new file.

    Chromeria hashes the original bytes and resolves target relative to the
    entry file (or the home directory). Only scalar id/target header fields
    are needed; no body or arbitrary YAML data reaches the ledger.
    """
    found = {}
    for root in roots:
        try:
            paths = Path(root).glob('*.md')
            for file in paths:
                if file.name.lower() == 'readme.md':
                    continue
                raw = file.read_bytes()
                text = raw.decode('utf-8-sig').replace('\r\n', '\n')
                lines = text.splitlines()
                if not lines or lines[0].strip() != '---':
                    continue
                fields = {}
                for line in lines[1:]:
                    if line.strip() == '---':
                        break
                    key, sep, val = line.partition(':')
                    if sep and key in ('id', 'target'):
                        val = val.strip()
                        if val.startswith('"'):
                            val = json.loads(val)
                        elif val.startswith("'") and val.endswith("'"):
                            val = val[1:-1].replace("''", "'")
                        fields[key] = val
                entry = _id(fields.get('id', file.stem))
                if not entry:
                    continue
                target = fields.get('target')
                if isinstance(target, str) and target:
                    target = Path(target).expanduser()
                    target = str((file.parent / target).resolve())
                else:
                    target = None
                key = (entry, hashlib.sha256(raw).hexdigest()[:12])
                value = (_path(target), 'resolved' if _path(target) else 'no_target')
                if key in found and found[key] != value:
                    found[key] = (None, 'ambiguous_target')
                else:
                    found[key] = value
        except (OSError, ValueError, UnicodeError):
            continue
    return found


def _native_reads(con):
    """Exact call id and path only; timestamp proximity is not identity."""
    found = defaultdict(set)
    for r in con.execute("SELECT id,session_key,native_id,target,detail_json FROM events"
                         " WHERE family IN ('read','skill_read')"):
        try:
            detail = json.loads(r['detail_json'] or '{}')
        except ValueError:
            continue
        path = detail.get('skill_path') if isinstance(detail, dict) else None
        path = path or r['target']
        if not _path(path):
            continue
        native = r['native_id']
        # Native adapters suffix a read's call id with :read or :<path>.
        for call in (native, native.removesuffix(':read'), native.removesuffix(':' + path)):
            found[(r['session_key'], call, path)].add(r['id'])
    return found


def import_source(con, source=None, libraries=()):
    """Import one consistent source snapshot. A failed refresh preserves prior data."""
    _ensure(con)
    status = source_status(source)
    if status != 'ok':
        con.execute('DELETE FROM cue_import')
        con.execute('INSERT INTO cue_import VALUES(?,?,NULL)', (status, time.time()))
        return {'status': status}
    with closing(_open(source)) as native:
        native.execute('BEGIN')
        moments = native.execute('SELECT id,received_at,harness,session,epoch,holdout,kind,'
                                 'tool_use_id,owners_json,cue_status FROM moments').fetchall()
        roots = set(libraries)
        for m in moments:
            try:
                owners = json.loads(m['owners_json'] or '{}')
                if isinstance(owners, dict):
                    roots.update(k for k in owners if _path(k))
            except ValueError:
                pass
        targets_by_root = {root: _targets([root]) for root in roots}
        target_cache = {}
        moment_targets = {}
        for m in moments:
            try:
                owners = json.loads(m['owners_json'] or '{}')
            except ValueError:
                owners = {}
            active = set(owners) if isinstance(owners, dict) else set()
            active.update(libraries)
            root_key = tuple(sorted(active))
            if root_key not in target_cache:
                mapping = {}
                for root in active:
                    for key, value in targets_by_root.get(root, {}).items():
                        if key in mapping and mapping[key] != value:
                            mapping[key] = (None, 'ambiguous_target')
                        else:
                            mapping[key] = value
                target_cache[root_key] = mapping
            moment_targets[m['id']] = target_cache[root_key]
        matches = _native_reads(con)
        con.execute('SAVEPOINT cue_refresh')
        try:
            for table in ('cue_candidates', 'cue_reads', 'cue_moments', 'cue_import'):
                con.execute('DELETE FROM ' + table)
            for m in moments:
                if not (_number(m['received_at']) and _id(m['harness']) and _id(m['session'])
                        and type(m['epoch']) is int and m['holdout'] in (0, 1)):
                    continue
                con.execute('INSERT INTO cue_moments VALUES(?,?,?,?,?,?,?,?,?)',
                            (m['id'],m['received_at'],m['harness'],m['session'],m['epoch'],
                             m['holdout'],_id(m['kind']),_id(m['tool_use_id']),_id(m['cue_status'])))
            ids = {r[0] for r in con.execute('SELECT id FROM cue_moments')}
            columns = {r[1] for r in native.execute('PRAGMA table_info(candidates)')}
            delivery = 'COALESCE(c.delivery,m.delivery)' if 'delivery' in columns else 'm.delivery'
            for c in native.execute(f'''SELECT c.id,c.moment_id,c.entry,c.version,c.score,c.outcome,
                    c.stage,c.served_at,d.stage AS delivery_stage FROM candidates c
                    JOIN moments m ON m.id=c.moment_id
                    LEFT JOIN deliveries d ON d.delivery={delivery} WHERE c.matcher='intent' '''):
                if (c['moment_id'] not in ids or not isinstance(c['entry'], str)
                        or not ENTRY_ID.fullmatch(c['entry']) or not _id(c['version'])):
                    continue
                target, target_status = moment_targets[c['moment_id']].get((c['entry'], c['version']),
                                                     (None, 'version_unavailable'))
                con.execute('INSERT INTO cue_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (c['id'],c['moment_id'],c['entry'],c['version'],
                             c['score'] if _number(c['score']) else None,
                             _id(c['outcome']),_id(c['stage']),c['served_at'],
                             _id(c['delivery_stage']),target,target_status))
            calls = {m['id']: m['tool_use_id'] for m in moments}
            for r in native.execute('SELECT harness,session,epoch,path,kind,moment_id,read_at'
                                     ' FROM reads WHERE pending_tool_use_id IS NULL'):
                if (r['moment_id'] not in ids or not _path(r['path']) or r['kind'] not in KINDS
                        or not _number(r['read_at'])):
                    continue
                key = (f"{r['harness']}:{r['session']}", calls.get(r['moment_id']), r['path'])
                evidence = matches.get(key, set())
                event_id = next(iter(evidence)) if len(evidence) == 1 else None
                con.execute('INSERT OR IGNORE INTO cue_reads VALUES(?,?,?,?,?,?,?,?)',
                            (*tuple(r), event_id))
            watermark = max((m['received_at'] for m in moments if _number(m['received_at'])), default=0)
            con.execute('INSERT INTO cue_import VALUES(?,?,?)', ('ok',time.time(),watermark))
            con.execute('RELEASE cue_refresh')
        except BaseException:
            con.execute('ROLLBACK TO cue_refresh')
            con.execute('RELEASE cue_refresh')
            raise
    return {'status': 'ok', **{t: con.execute('SELECT count(*) FROM cue_' + t).fetchone()[0]
                              for t in ('moments','candidates','reads')}}


def health(con=None, source=None):
    status = source_status(source)
    if status != 'ok':
        return {'status': status}
    if con is None:
        return {'status': 'not_imported'}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='cue_import'").fetchone():
        return {'status': 'not_imported'}
    row = con.execute('SELECT * FROM cue_import').fetchone()
    return dict(row) if row else {'status': 'not_imported'}


def report(con, machine=None, check_source=True, source=None):
    from .fleet import machine_name
    state = health(con, source) if check_source else _stored_health(con)
    result = {'status': state['status'], 'import': state, 'window_seconds': WINDOW_SECONDS,
              'holdout_unit': 'session',
              'interpretation': 'Observational session-wide Cue holdout; read evidence is not proof of following a procedure or a per-procedure causal effect.',
              'window': 'After the moment, before the next user message or context epoch, capped at 30 minutes and the imported source watermark. Earlier reads are separate; incomplete windows are pending.',
              'groups': [], 'gaps': {}}
    if state['status'] != 'ok':
        return result
    moments = {r['id']: dict(r) for r in con.execute('SELECT * FROM cue_moments')}
    boundaries = defaultdict(list)
    by_session = defaultdict(list)
    for m in moments.values():
        by_session[(m['harness'],m['session'])].append(m)
    for key, ms in by_session.items():
        prior_epoch = None
        for m in sorted(ms, key=lambda m: (m['at'], m['id'])):
            if m['kind'] == 'user_message' or (prior_epoch is not None and m['epoch'] != prior_epoch):
                boundaries[key].append(m['at'])
            prior_epoch = m['epoch']
    reads = defaultdict(list)
    provenance = Counter()
    for r in con.execute('SELECT * FROM cue_reads ORDER BY at'):
        reads[(r['harness'],r['session'],r['epoch'],r['path'])].append(dict(r))
        provenance['native_and_contextmd' if r['native_event_id'] else 'contextmd_only'] += 1
    versions = {r[0]: r[1] or 'unknown' for r in con.execute('SELECT session_key,agentsmd_version FROM sessions')}
    groups = {}
    gaps = Counter()
    for c in con.execute('SELECT * FROM cue_candidates'):
        m = moments[c['moment_id']]
        if not c['target']:
            gaps[c['target_status']] += 1
            continue
        key = (c['entry'],m['harness'],machine or machine_name(),
               versions.get(f"{m['harness']}:{m['session']}", 'unknown'))
        if key not in groups:
            groups[key] = {'entry': key[0], 'harness': key[1], 'machine': key[2],
                           'agentsmd_version': key[3], 'outcomes': Counter(), 'arms': {}}
        g = groups[key]
        g['outcomes'][c['outcome']] += 1
        arm = 'holdout' if c['outcome'] == 'holdout' and m['holdout'] == 1 else (
            'served' if c['outcome'] in SERVED and m['holdout'] == 0 else None)
        if arm is None:
            continue
        a = g['arms'].setdefault(arm, {'opportunities': 0, 'sessions': set(), 'read': 0,
            'full': 0, 'partial': 0, 'already_read': 0, 'already_full': 0, 'already_partial': 0,
            'pending_window': 0, 'native_matched_reads': 0})
        a['opportunities'] += 1
        a['sessions'].add(m['session'])
        bs = boundaries[(m['harness'],m['session'])]
        index = bisect_right(bs, m['at'])
        end = min(m['at'] + WINDOW_SECONDS, bs[index] if index < len(bs) else math.inf)
        watermark = state.get('watermark') or 0
        a['pending_window'] += end > watermark
        end = min(end, math.nextafter(watermark, math.inf))
        rs = reads[(m['harness'],m['session'],m['epoch'],c['target'])]
        times = [r['at'] for r in rs]
        earlier = rs[:bisect_right(times,m['at'])]
        if earlier:
            a['already_read'] += 1
            a['already_full' if any(r['kind'] in FULL for r in earlier) else 'already_partial'] += 1
        after = rs[bisect_right(times,m['at']):bisect_left(times,end)]
        if after:
            a['read'] += 1
            a['full' if any(r['kind'] in FULL for r in after) else 'partial'] += 1
            a['native_matched_reads'] += sum(r['native_event_id'] is not None for r in after)
    for g in groups.values():
        for a in g['arms'].values():
            a['sessions'] = len(a['sessions'])
            a['read_rate'] = a['read'] / a['opportunities']
        g['outcomes'] = dict(g['outcomes'])
    result.update(groups=sorted(groups.values(),key=lambda g:(g['entry'],g['harness'],g['agentsmd_version'])),
                  gaps=dict(gaps), read_sources=dict(provenance),
                  cue_statuses=dict(Counter(m['cue_status'] or 'unknown' for m in moments.values())))
    return result


def _stored_health(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='cue_import'").fetchone():
        return {'status': 'not_imported'}
    r = con.execute('SELECT * FROM cue_import').fetchone()
    return dict(r) if r else {'status': 'not_imported'}


def render(result):
    lines = [f"Cue procedure reads: {result['status']}", result['interpretation'], result['window']]
    for g in result['groups']:
        lines.append(f"{g['entry']} | {g['harness']} | {g['machine']} | AgentsMD {g['agentsmd_version']}")
        for arm,a in g['arms'].items():
            lines.append(f"  {arm}: {a['read']}/{a['opportunities']} read, {a['sessions']} sessions; "
                         f"full {a['full']}, partial {a['partial']}, earlier {a['already_read']}, "
                         f"pending windows {a['pending_window']}")
    if result['gaps']:
        lines.append('Target gaps: ' + json.dumps(result['gaps'],sort_keys=True))
    return '\n'.join(lines)
