"""Operational launch outbox, independent of model events and bounded display tails.

Rows share the assignment transaction. One immutable, at-most-500-event publication
is retried until acknowledged; only then may its rows be removed. Retained executor
storage is part of the fenced swarm handoff, including this outbox and its receipt.
"""
import hashlib
import json

from .contracts_v3 import canonical_json


SCHEMA = '''
CREATE TABLE IF NOT EXISTS launch_journal(seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS launch_journal_state(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
'''


def state(db):
    row = db.execute('SELECT body FROM launch_journal_state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else {}


def save(db, value):
    db.execute('INSERT OR REPLACE INTO launch_journal_state VALUES (1,?)', (canonical_json(value).decode(),))


def append(db, event):
    row = db.execute('INSERT INTO launch_journal(payload) VALUES (?)', (canonical_json(event).decode(),))
    return dict(event, launch_seq=row.lastrowid)


def migrate_tail(db, runtime):
    """Import only a legacy tail the old length cursor has not already submitted.

    Legacy history already trimmed away cannot be reconstructed. Do not relabel or
    republish its acknowledged/submitted prefix when upgrading a retained runtime.
    """
    value = state(db)
    if value.get('legacy_imported'):return
    rows = getattr(runtime, 'launch_triggers', None) or []
    n = (getattr(runtime, 'events_seen', None) or {}).get('launch_triggers', 0)
    if len(rows) < n:n = 0
    for event in rows[n:]:
        if 'launch_seq' not in event:append(db, event)
    value['legacy_imported'] = True
    save(db, value)


def flush(executor, publisher, arm):
    """Bounded reads; retry identical bytes/identity even after submit/ack interruption."""
    with executor.tx() as (db, _):
        value = state(db)
        pending = value.get('pending')
        if pending is None:
            rows = db.execute('SELECT seq,payload FROM launch_journal ORDER BY seq LIMIT 500').fetchall()
            if not rows:return
            events = [dict(json.loads(r['payload']), kind='launch-trigger', launch_seq=r['seq']) for r in rows]
            pending = dict(identity='harness-events-%s-launch-%d-%d' % (arm, rows[0]['seq'], rows[-1]['seq']),
                           last=rows[-1]['seq'], payload=dict(at=events[-1]['at'], arm=arm, events=events))
            value['pending'] = pending
            save(db, value)
    # No storage transaction is held across publisher I/O. The runtime controller
    # serializes flushes; the frozen pending payload excludes concurrent appends.
    publisher.submit(pending['identity'], pending['payload'])
    if not publisher.require_ack(pending['identity']):return
    with executor.tx() as (db, _):
        value = state(db)
        if value.get('pending') != pending:raise ValueError('launch publication changed')
        value['receipt'] = dict(identity=pending['identity'], through=pending['last'],
                                sha256=hashlib.sha256(canonical_json(pending['payload'])).hexdigest())
        value.pop('pending')
        db.execute('DELETE FROM launch_journal WHERE seq<=?', (pending['last'],))
        save(db, value)


def snapshot(db):
    """Bounded handoff integrity witness; telemetry never enters model packets."""
    value = state(db)
    if not value:return None  # an untouched legacy executor keeps its old snapshot shape
    digest = hashlib.sha256(); count = 0
    for row in db.execute('SELECT seq,payload FROM launch_journal ORDER BY seq'):
        digest.update(canonical_json([row['seq'], row['payload']])); count += 1
    seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='launch_journal'").fetchone()
    return dict(state_sha256=hashlib.sha256(canonical_json(value)).hexdigest(),
                rows_sha256=digest.hexdigest(), rows=count, sequence=seq[0] if seq else 0)
