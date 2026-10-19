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
import random
import sqlite3
import time

from . import privacy

WINDOW_SECONDS = 1800
SERVED = frozenset({'injected', 'truncated', 'unverified', 'awaiting_evidence'})
COMPARISON = 'first_above_threshold_per_session_entry_epoch'
INTERVAL_METHOD = 'session_cluster_bootstrap_percentile_95_2000'
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
    # Integers are finite without a potentially overflowing float conversion.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _targets(roots):
    """Resolve only version-matching files, never infer an old target from a new file.

    Chromeria hashes the original bytes and resolves target relative to the
    entry file (or the home directory). Only scalar id/target header fields
    are needed; no body or arbitrary YAML data reaches the ledger.
    """
    found = {}
    for root in roots:
        try:
            paths = list(Path(root).glob('*.md'))
        except OSError:
            continue
        for file in paths:
            try:
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
                # Only candidates for this file remain unresolved, not its siblings.
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


def _record_gap(con, status):
    con.execute('DELETE FROM cue_import')
    con.execute('INSERT INTO cue_import VALUES(?,?,NULL)', (status, time.time()))
    return {'status': status}


def import_source(con, source=None, libraries=()):
    """Isolate Cue failure from other pending imports, placement and identity writes."""
    _ensure(con)
    con.execute('SAVEPOINT cue_attempt')
    try:
        return _import_snapshot(con, source, libraries)
    except (sqlite3.Error, TypeError) as exc:
        con.execute('ROLLBACK TO cue_attempt')
        # Exception messages can contain source data; retain only a safe gap category.
        return _record_gap(con, 'schema_drift' if isinstance(exc, TypeError)
                           else 'unreadable_database')
    except BaseException:
        con.execute('ROLLBACK TO cue_attempt')
        raise
    finally:
        con.execute('RELEASE cue_attempt')


def _import_snapshot(con, source, libraries):
    """Import one consistent source snapshot. A failed refresh preserves prior data."""
    status = source_status(source)
    if status != 'ok':
        return _record_gap(con, status)
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
                if not (_number(m['received_at']) and _id(m['harness']) and _id(m['session'])):
                    continue
                con.execute('INSERT INTO cue_moments VALUES(?,?,?,?,?,?,?,?,?)',
                            (m['id'],m['received_at'],m['harness'],m['session'],
                             m['epoch'] if type(m['epoch']) is int else None,
                             m['holdout'] if m['holdout'] in (0, 1) else None,
                             _id(m['kind']),_id(m['tool_use_id']),_id(m['cue_status'])))
            ids = {r[0] for r in con.execute('SELECT id FROM cue_moments')}
            eligible = {m['id'] for m in moments if type(m['epoch']) is int and m['holdout'] in (0, 1)}
            columns = {r[1] for r in native.execute('PRAGMA table_info(candidates)')}
            delivery = 'COALESCE(c.delivery,m.delivery)' if 'delivery' in columns else 'm.delivery'
            for c in native.execute(f'''SELECT c.id,c.moment_id,c.entry,c.version,c.score,c.outcome,
                    c.stage,c.served_at,d.stage AS delivery_stage FROM candidates c
                    JOIN moments m ON m.id=c.moment_id
                    LEFT JOIN deliveries d ON d.delivery={delivery} WHERE c.matcher='intent' '''):
                if (c['moment_id'] not in ids or c['moment_id'] not in eligible or not isinstance(c['entry'], str)
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


def _cluster_interval(sessions):
    """Resample whole harness sessions, keeping their entry/epoch opportunities together."""
    clusters = sorted(sessions.values())
    if len(clusters) < 2:
        return None
    rng = random.Random(0)
    rates = []
    for _ in range(2000):
        sample = rng.choices(clusters, k=len(clusters))
        rates.append(sum(r for r, n in sample) / sum(n for r, n in sample))
    rates.sort()
    return [rates[49], rates[1949]]


def _bucket():
    return {**{k: 0 for k in ARM_COUNTS if k != 'sessions'}, 'sessions': {}}


def _add(bucket, session, metrics):
    for k, value in metrics.items():
        bucket[k] += value
    cluster = bucket['sessions'].setdefault(session, [0, 0])
    cluster[0] += metrics['read']
    cluster[1] += 1


def _finish_buckets(container):
    for section in ('arms', 'repeats', 'excluded'):
        for bucket in container[section].values():
            bucket['interval95'] = _cluster_interval(bucket['sessions'])
            bucket['sessions'] = len(bucket['sessions'])
            bucket['read_rate'] = bucket['read'] / bucket['opportunities']


def _carry_prints(con):
    """Pair a unit's first carried judgment with its first eventual delivery.

    A queue row is not a delivery. Intermediate deduplication, expiration and
    further scoring rows do not replace the unit; the print may be much later.
    Other first outcomes and unavailable first targets still consume the slot.
    """
    first, pending, resolved, printed = set(), {}, {}, set()
    for c in con.execute('''SELECT c.*,m.harness,m.session,m.epoch,m.holdout
            FROM cue_candidates c JOIN cue_moments m ON m.id=c.moment_id
            WHERE c.score IS NOT NULL AND c.outcome NOT IN ('below_threshold','not_scored')
              AND typeof(m.epoch)='integer' AND m.holdout IN (0,1)
            ORDER BY m.at,m.id,c.id'''):
        unit = (c['harness'],c['session'],c['entry'],c['epoch'])
        if unit not in first:
            first.add(unit)
            if c['outcome'] == 'carried' and c['target'] and c['holdout'] == 0:
                pending[unit] = c['id']
        elif unit in pending and c['holdout'] == 0 and c['outcome'] in SERVED | {'hidden'}:
            resolved[pending.pop(unit)] = c
            printed.add(c['id'])
    return resolved, printed


def report(con, machine=None, check_source=True, source=None):
    from .fleet import machine_name
    state = health(con, source) if check_source else _stored_health(con)
    result = {'status': state['status'], 'import': state, 'window_seconds': WINDOW_SECONDS,
              'holdout_unit': 'session', 'comparison': COMPARISON, 'interval_method': INTERVAL_METHOD,
              'interpretation': 'Compare first above-threshold units per harness session, entry and context epoch. A first carried judgment uses its eventual print in that epoch; a carry never printed is excluded separately. Hidden and other undelivered first judgments are excluded; repeats are separate. Holdout withholds all Cue entries for a session. Read evidence proves neither following a procedure nor a per-procedure causal effect. Per-harness headlines expose arm mix differences; small holdout samples limit precision.',
              'window': 'After actual delivery (the print moment for a resolved carry), or the first judgment for holdout/excluded diagnostics, before every next user message (even with missing Cue metadata) or known context epoch change, capped at 30 minutes and the imported watermark. Earlier reads are separate; incomplete windows are pending.',
              'interval_note': '95% percentile intervals resample whole observed harness sessions 2000 times with a fixed seed. Fewer than two sessions gives no interval; small samples and all-equal outcomes can give unstable or degenerate intervals. These are descriptive, not causal.',
              'summary': {'arms': {}, 'repeats': {}, 'excluded': {}},
              'sessions_by_arm': {}, 'by_harness': {}, 'groups': [], 'gaps': {}}
    if state['status'] != 'ok':
        return result
    moments = {r['id']: dict(r) for r in con.execute('SELECT * FROM cue_moments')}
    boundaries = {}
    by_session = defaultdict(list)
    for m in moments.values():
        by_session[(m['harness'],m['session'])].append(m)
    for key, ms in by_session.items():
        prior_epoch = None
        flags = {}
        for m in sorted(ms, key=lambda m: (m['at'], m['id'])):
            known_epoch = type(m['epoch']) is int
            epoch_changed = known_epoch and prior_epoch is not None and m['epoch'] != prior_epoch
            if m['kind'] == 'user_message' or epoch_changed:
                missing = not known_epoch or m['holdout'] not in (0, 1)
                # If another valid boundary has this timestamp, missing metadata was not needed.
                flags[m['at']] = flags.get(m['at'], True) and missing
            if known_epoch:
                prior_epoch = m['epoch']
        boundaries[key] = (sorted(flags), flags)
    reads = defaultdict(list)
    provenance = Counter()
    for r in con.execute('SELECT * FROM cue_reads ORDER BY at'):
        reads[(r['harness'],r['session'],r['epoch'],r['path'])].append(dict(r))
        provenance['native_and_contextmd' if r['native_event_id'] else 'contextmd_only'] += 1
    versions = {r[0]: r[1] or 'unknown' for r in con.execute('SELECT session_key,agentsmd_version FROM sessions')}
    groups, seen = {}, set()
    carry_prints, printed = _carry_prints(con)
    coverage = defaultdict(set)
    gaps = Counter()
    # Source ordering, not import order or the first successful delivery, defines the judgment.
    for c in con.execute('SELECT c.* FROM cue_candidates c JOIN cue_moments m ON m.id=c.moment_id ORDER BY m.at,m.id,c.id'):
        m = moments[c['moment_id']]
        above = (c['score'] is not None and c['outcome'] not in ('below_threshold','not_scored')
                 and type(m['epoch']) is int and m['holdout'] in (0, 1))
        unit = (m['harness'],m['session'],c['entry'],m['epoch'])
        first = above and unit not in seen
        if above:
            seen.add(unit)
        if not c['target']:
            gaps[c['target_status']] += 1
            continue
        key = (c['entry'],m['harness'],machine or machine_name(),
               versions.get(f"{m['harness']}:{m['session']}", 'unknown'))
        if key not in groups:
            groups[key] = {'entry': key[0], 'harness': key[1], 'machine': key[2],
                           'agentsmd_version': key[3], 'outcomes': Counter(),
                           'arms': {}, 'repeats': {}, 'excluded': {}}
        g = groups[key]
        g['outcomes'][c['outcome']] += 1
        if not above:
            continue
        arm = 'holdout' if m['holdout'] == 1 else 'served'
        # An inconsistent holdout row cannot turn a serving-enabled session into holdout.
        if arm == 'served' and c['outcome'] == 'holdout':
            continue
        coverage[arm].add((m['harness'],m['session']))
        harness_summary = result['by_harness'].setdefault(m['harness'],
            {'arms': {}, 'repeats': {}, 'excluded': {}, 'sessions_by_arm': {}})
        # The print belongs to its original first unit, not to a second opportunity.
        if c['id'] in printed:
            continue
        observation = carry_prints.get(c['id'], c)
        if not observation['target']:
            continue  # The print's source row records its target gap exactly once.
        outcome = observation['outcome']
        if not first:
            section = 'repeats'
        elif arm == 'served' and outcome not in SERVED:
            section = 'excluded'
            arm = ('hidden' if outcome == 'hidden' else 'carry_unprinted'
                   if outcome in ('carried','carry_expired') else 'not_served')
        elif arm == 'holdout' and c['outcome'] != 'holdout':
            section, arm = 'excluded', 'not_served'
        else:
            section = 'arms'
        m = moments[observation['moment_id']]
        session = (m['harness'],m['session'])
        bs, flags = boundaries[session]
        index = bisect_right(bs, m['at'])
        boundary = bs[index] if index < len(bs) else math.inf
        cap = m['at'] + WINDOW_SECONDS
        end = min(cap, boundary)
        watermark = state.get('watermark') or 0
        metrics = {k: 0 for k in ARM_COUNTS if k != 'sessions'}
        metrics['opportunities'] = 1
        metrics['carry_prints'] = int(c['id'] in carry_prints)
        metrics['pending_window'] = int(end > watermark)
        metrics['boundary_without_cue_metadata'] = int(boundary <= cap and flags.get(boundary, False))
        end = min(end, math.nextafter(watermark, math.inf))
        rs = reads[(m['harness'],m['session'],m['epoch'],observation['target'])]
        times = [r['at'] for r in rs]
        earlier = rs[:bisect_right(times,m['at'])]
        if earlier:
            metrics['already_read'] = 1
            metrics['already_full' if any(r['kind'] in FULL for r in earlier) else 'already_partial'] = 1
        after = rs[bisect_right(times,m['at']):bisect_left(times,end)]
        if after:
            metrics['read'] = 1
            metrics['full' if any(r['kind'] in FULL for r in after) else 'partial'] = 1
            metrics['native_matched_reads'] = sum(r['native_event_id'] is not None for r in after)
        for container in (g,result['summary'],harness_summary):
            _add(container[section].setdefault(arm,_bucket()),session,metrics)
    for g in groups.values():
        _finish_buckets(g)
        g['outcomes'] = dict(g['outcomes'])
    _finish_buckets(result['summary'])
    result['sessions_by_arm'] = {arm: len(sessions) for arm,sessions in coverage.items()}
    for harness,container in result['by_harness'].items():
        _finish_buckets(container)
        container['sessions_by_arm'] = {
            arm: sum(h == harness for h,s in sessions) for arm,sessions in coverage.items()
            if any(h == harness for h,s in sessions)}
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
    lines = [f"Cue procedure reads: {result['status']}", result['interpretation'], result['window'], result['interval_note']]
    lines.append('Observed session assignment coverage: ' + json.dumps(result['sessions_by_arm'],sort_keys=True))
    def buckets(container, prefix=''):
        for section in ('arms', 'repeats', 'excluded'):
            for arm,a in container[section].items():
                interval = a['interval95']
                ci = (f"{interval[0]:.1%} to {interval[1]:.1%}" if interval is not None else 'unavailable (<2 sessions)')
                label = 'first judgments' if section == 'arms' else section
                lines.append(f"{prefix}{label} {arm}: {a['read']}/{a['opportunities']} ({a['read_rate']:.1%}) read, {a['sessions']} sessions, 95% interval {ci}; "
                             f"full {a['full']}, partial {a['partial']}, earlier {a['already_read']}, pending {a['pending_window']}, "
                             f"carry prints {a['carry_prints']}, boundaries needing missing metadata {a['boundary_without_cue_metadata']}")
    buckets(result['summary'])
    for harness,container in sorted(result['by_harness'].items()):
        lines.append(f"Harness {harness}; observed sessions: {json.dumps(container['sessions_by_arm'],sort_keys=True)}")
        buckets(container, '  ')
    for g in result['groups']:
        lines.append(f"{g['entry']} | {g['harness']} | {g['machine']} | AgentsMD {g['agentsmd_version']}")
        buckets(g, '  ')
    if result['gaps']:
        lines.append('Target gaps: ' + json.dumps(result['gaps'],sort_keys=True))
    return '\n'.join(lines)


EXPORT_FIELDS = frozenset({'status', 'window_seconds', 'holdout_unit', 'groups',
                           'gaps', 'read_sources', 'cue_statuses', 'summary', 'comparison', 'interval_method',
                           'sessions_by_arm', 'by_harness'})
ARM_COUNTS = frozenset({'opportunities', 'sessions', 'read', 'full', 'partial',
    'already_read', 'already_full', 'already_partial', 'pending_window', 'native_matched_reads', 'boundary_without_cue_metadata', 'carry_prints'})


def export(con, machine=None):
    """Optional aggregate extension; old ledgers retain their existing export shape."""
    if _stored_health(con)['status'] == 'not_imported':
        return None
    value = report(con, machine=machine, check_source=False)
    return {k: value.get(k, {}) for k in EXPORT_FIELDS}


def validate_export(value):
    """Fail closed at the fleet boundary: no paths, moments or free-text extras."""
    def require(ok):
        if not ok:
            raise ValueError('invalid Cue aggregate export')

    def counts(mapping, allowed=None):
        require(isinstance(mapping, dict))
        for k, v in mapping.items():
            require(isinstance(k, str) and re.fullmatch(r'[a-z_]{1,64}', k)
                    and (allowed is None or k in allowed)
                    and type(v) is int and 0 <= v <= 2**53)

    def validate_buckets(container):
        require(isinstance(container, dict))
        for section, allowed in (('arms', {'served','holdout'}), ('repeats', {'served','holdout'}),
                                 ('excluded', {'hidden','not_served','carry_unprinted'})):
            require(isinstance(container.get(section), dict) and set(container[section]) <= allowed)
            for arm in container[section].values():
                require(isinstance(arm, dict) and set(arm) == ARM_COUNTS | {'read_rate','interval95'})
                counts({k: arm[k] for k in ARM_COUNTS}, ARM_COUNTS)
                require(_number(arm['read_rate']) and 0 <= arm['read_rate'] <= 1)
                interval = arm['interval95']
                require(interval is None or (isinstance(interval, list) and len(interval) == 2
                        and all(_number(v) and 0 <= v <= 1 for v in interval) and interval[0] <= interval[1]))
                require(arm['read'] == arm['full'] + arm['partial'] <= arm['opportunities'])
                require(arm['already_read'] == arm['already_full'] + arm['already_partial'] <= arm['opportunities'])

    require(isinstance(value, dict) and set(value) == EXPORT_FIELDS)
    require(isinstance(value['status'], str) and value['status'] in {'ok','missing_database','schema_drift','no_cue',
                                'unreadable_database','not_imported'})
    require(value['window_seconds'] == WINDOW_SECONDS and value['holdout_unit'] == 'session')
    require(value['comparison'] == COMPARISON and value['interval_method'] == INTERVAL_METHOD)
    require(isinstance(value['summary'], dict) and set(value['summary']) == {'arms','repeats','excluded'})
    counts(value['sessions_by_arm'], {'served','holdout'})
    require(isinstance(value['by_harness'], dict)
            and set(value['by_harness']) <= {'codex','claude','grok','opencode'})
    for container in value['by_harness'].values():
        require(isinstance(container, dict) and set(container) == {'arms','repeats','excluded','sessions_by_arm'})
        counts(container['sessions_by_arm'], {'served','holdout'})
        validate_buckets(container)
    counts(value['gaps'], {'version_unavailable','no_target','ambiguous_target'})
    counts(value['read_sources'], {'native_and_contextmd','contextmd_only'})
    counts(value['cue_statuses'])
    require(isinstance(value['groups'], list))
    for g in value['groups']:
        require(isinstance(g, dict) and set(g) == {'entry','harness','machine',
                                                    'agentsmd_version','outcomes','arms','repeats','excluded'})
        require(isinstance(g['entry'], str) and ENTRY_ID.fullmatch(g['entry']))
        require(isinstance(g['harness'], str) and g['harness'] in {'codex','claude','grok','opencode'})
        require(isinstance(g['machine'], str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', g['machine']))
        require(isinstance(g['agentsmd_version'], str)
                and re.fullmatch(r'(?:[0-9]+\.[0-9]+\.[0-9]+|unknown)', g['agentsmd_version']))
        counts(g['outcomes'])
        validate_buckets(g)
    validate_buckets(value['summary'])
    return value
