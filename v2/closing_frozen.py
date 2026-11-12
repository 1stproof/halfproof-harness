"""Explicit report recovery for a terminal production swarm, on separate storage.

Also accepts a run that was cleanly paused (sealed controller handoff) and whose
allocation then ended before any owner restored it: the sealed handoff in the
durable root supplies the ledger and clocks, the durable swarm publication
supplies the board, bound to the seal by board cursor and executor state.

This is not cohort restore. Original jobs/results/boards are immutable evidence.
Only a copied ledger may enter report mode, with all prior attempts and holds.
"""
import argparse
import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .accounting import AccountingStore
from .control import atomic_json
from .deployment import DeploymentRefused
from .durability import read_publication
from .pause import RunClock

ARM = 'mixed-swarm'
TERMINAL = {'FAILED', 'OUT_OF_MEMORY', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'COMPLETED'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def latest(root, prefix):
    rows = [p for p in (root/'journal').glob(prefix+'*.json')
            if p.stem[len(prefix):].isdigit()]
    if not rows:
        raise DeploymentRefused('saved publication missing: '+prefix)
    return max(rows, key=lambda p: int(p.stem[len(prefix):]))


def scheduler(job):
    if not re.fullmatch(r'[0-9]+', str(job)):
        raise DeploymentRefused('numeric original job required')
    r = subprocess.run(['sacct', '-n', '-P', '-j', str(job), '-o', 'JobIDRaw,State,ExitCode,NodeList,ElapsedRaw'],
                       capture_output=True, text=True, timeout=30, check=True)
    rows = [x.split('|') for x in r.stdout.splitlines() if x.strip()]
    owner = [x for x in rows if x[0] == str(job)]
    if len(owner) != 1 or owner[0][1].split('+')[0] not in TERMINAL:
        raise DeploymentRefused('original allocation not authoritatively terminal')
    if any(x[1].split('+')[0] not in TERMINAL for x in rows):
        raise DeploymentRefused('original step not terminal')
    return dict(job=str(job), rows=rows, elapsed_seconds=int(owner[0][4]))


def failed_delivery(data):
    """A failed run, or a closed run whose only delivery is the administrative fallback."""
    result, closure = data['result'], data.get('closure')
    if result['status'] == 'failed':
        return closure is None
    return (result['status'] == 'closed' and result.get('failure') is None
            and isinstance(closure, dict) and closure.get('arm_id') == ARM
            and closure.get('problem_id') == data['manifest']['problem_id']
            and closure.get('model_delivery') == 'failed'
            and (closure.get('selected') or {}).get('authorship') == 'administrative-fallback')


def validate_saved(data):
    if data.get('source_kind')=='sealed-pause':return validate_paused(data)
    m, s, checkpoint = data['manifest'], data['swarm'], data['checkpoint']
    failed_snapshot = data.get('source_kind') == 'failed-unfrozen'
    salvaged_snapshot = data.get('source_kind') == 'salvaged-terminal'
    c = checkpoint['controller']; a = checkpoint['accounting']; b = json.loads(s['board']['snapshot'])
    if (m.get('profile') != 'production' or m.get('live_arms') != [ARM]
            or data['status']['phase'] not in (('running', 'closed') if failed_snapshot else ('closed',)) or data['status'].get('in_progress')
            or data['status']['job'] != data['scheduler']['job']
            or not failed_delivery(data)
            or data['status']['cohort'] != m['cohort_id']
            or c['cohort'] != m['cohort_id'] or b['cohort_id'] != m['cohort_id']
            or b['problem_id'] != m['problem_id'] or b['arm_id'] != ARM
            or b['contracts_sha256'] != m['contracts_sha256']
            or c['contracts_sha256'] != m['contracts_sha256']
            or s['original_start'] != c['original_start']
            or s['deadline'] != c['deadline']
            or sha(s['board']['snapshot'].encode()) != s['board']['sha256']
            or (not failed_snapshot and (s['mode'] != 'terminal' or s['executor']['mode'] != 'frozen'
                or s['slots'] or not any(x['kind'] == 'freeze' for x in b['records'])))):
        raise DeploymentRefused('not a matching terminal frozen production swarm')
    for artifact in b['artifacts']:
        if sha(base64.b64decode(artifact['base64'], validate=True)) != artifact['sha256']:
            raise DeploymentRefused('frozen artifact changed')
    ids = [r['id'] for r in b['records']]
    artifacts = {x['sha256'] for x in b['artifacts']}
    if len(ids) != len(set(ids)) or ids != sorted(ids) or any(
            r['artifact'] and r['artifact'] not in artifacts for r in b['records']):
        raise DeploymentRefused('frozen board references inconsistent')
    tables = a['tables']; settings = {r['key']: r['value'] for r in tables['settings']}
    for key in ('blocked', 'cohort'):
        settings[key] = json.loads(settings[key])
    if (settings.get('blocked') != {'reason': 'controller-shutdown'}
            or ('operator-paused' in settings and not salvaged_snapshot) or tables['lookup_locks'] or tables['waiters']
            or (not failed_snapshot and any(r['slot'] or r['reservation'] or r['lookup_active']
                   or r['state'] not in ('settled', 'unresolved') for r in tables['attempts']))
            or any(r['pause'] for r in tables['arms'])
            or (not failed_snapshot and next(r for r in tables['arms'] if r['id'] == ARM)['mode'] != 'terminal')
            or any(r['kind'] == 'accounting-anomaly' for r in tables['events'])):
        raise DeploymentRefused('unsafe or unrelated accounting fence')
    registration = settings['cohort']
    if (registration['id'] != m['cohort_id'] or registration['problem'] != m['problem_id']
            or registration['cap'] != m['cap_microusd']):
        raise DeploymentRefused('accounting identity differs')
    if failed_snapshot:
        proof = data.get('failed_owner', {})
        if (data.get('scientific_freeze') is not False
                or proof.get('job') != data['scheduler']['job']
                or proof.get('state') not in TERMINAL - {'COMPLETED'}
                or not proof.get('ended_at') or proof['ended_at'] < data['status']['observed_at']
                or proof.get('shutdown_checkpoint') != data['selected_publications'][1]
                or proof.get('checkpoint_acknowledged') is not True
                or data['result'].get('status') != 'failed'
                or data['result'].get('original_start') != c['original_start']
                or data['result'].get('deadline') != c['deadline']):
            raise DeploymentRefused('failed snapshot requires ended owner and acknowledged shutdown')
    clock = c['operator_clock']
    if salvaged_snapshot:
        st = data['status']; salvage = data.get('salvage') or {}
        proof = salvage.get('checkpoint') or {}
        identity = proof.get('identity', '')
        current = Path(data['selected_publications'][1]).stem
        if (settings.get('operator-paused') != 'true' or data.get('scientific_freeze') is not True
                or data['result'].get('failure') != 'InterruptedError'
                or data['result'].get('stop_signal') != 'SIGTERM'
                or ARM not in data['result'].get('frozen', [])
                or data['result'].get('original_start') != c['original_start']
                or data['result'].get('deadline') != c['deadline']
                or proof.get('acknowledged') is not True
                or not re.fullmatch(r'cohort-checkpoint-[0-9]+', identity)
                or not re.fullmatch(r'cohort-checkpoint-[0-9]+', current)
                or int(identity.rsplit('-', 1)[1]) > int(current.rsplit('-', 1)[1])
                or type(st.get('paused_since')) not in (int, float)
                or st['observed_at'] < st['paused_since']
                or abs(st['excluded_seconds'] - clock['excluded'] -
                       (st['observed_at'] - st['paused_since'])) > 1e-3):
            raise DeploymentRefused('salvaged terminal requires acknowledged frozen shutdown and exact paused clock evidence')
    if clock['since'] is not None or (not salvaged_snapshot and clock['excluded'] != data['status']['excluded_seconds']):
        raise DeploymentRefused('scientific clock mismatch')
    if sha(data['problem'].encode()) != m['problem']['sha256']:
        raise DeploymentRefused('problem changed')
    return data


def executor_digest(executor):
    """The swarm publication precedes the seal's last launch-journal flush."""
    from .cohort_restore import digest
    return digest({k:v for k,v in executor.items() if k!='launch_journal'})


def failed_resume(data):
    """Same-job replacement resume that died restoring the seal (P10 1842949,
    22:14 2026-10-13): nothing was published after the prepared seal."""
    f, st, r = data.get('failed_resume'), data['status'], data.get('result') or {}
    return (isinstance(f, dict) and st['phase'] == 'closed' and st.get('error') == 'DurabilityUnavailable'
            and r.get('status') == 'failed' and r.get('failure') == 'DurabilityUnavailable'
            and st.get('ack') == data['handoff']['control_ack']
            and f.get('last_event') == dict(event='resume-prepared', handoff=data['handoff']['sha256'])
            and f.get('journal_after_seal') == [])


def resolved_anomaly(event, attempts):
    """P10: an estimate-over-bound hold whose attempt later settled within its bound."""
    e = json.loads(event['payload']); r = attempts.get(e.get('attempt'))
    return (event['kind'] == 'accounting-anomaly' and e.get('reason') == 'estimate-over-bound' and r is not None
            and r['state'] == 'settled' and r['overrun'] == 0 and r['liability'] == 0 and 0 <= r['settled'] <= r['bound'])


def board_bound(s, b, cursor):
    """The seal's board cursor is the swarm's last synced record. Posts after that
    sync but inside the same hashed publication (P04: controller worker-result
    1516 over cursor 1515) belong to the saved board when the publication's own
    cursor equals the seal's; record ids stay contiguous from 1."""
    n = len(b['records']); top = max((r['id'] for r in b['records']), default=0)
    if n == cursor and top == cursor:
        return True
    return 'board_cursor' in s and s['board_cursor'] == cursor and n > cursor and top == n


def validate_paused(data):
    m, s, checkpoint = data['manifest'], data['swarm'], data['checkpoint']
    c = checkpoint['controller']; a = checkpoint['accounting']; b = json.loads(s['board']['snapshot'])
    h = data['handoff']; st = data['status']; clock = c['operator_clock']
    if (m.get('profile') != 'production' or m.get('live_arms') != [ARM]
            or (st['phase'] != 'paused' and not failed_resume(data)) or st['job'] != data['scheduler']['job']
            or st.get('in_progress') not in (None, h['control_ack'])
            or st['cohort'] != m['cohort_id'] or h['status'] != 'prepared'
            or h['record_digest'] != h['sha256'] or h['envelope_sha256'] != h['sha256']
            or h['record_root'] != m['local_root'] or h['record_durable'] != data['source_root']
            or h['record_cohort'] != m['cohort_id']
            or c['cohort'] != m['cohort_id'] or b['cohort_id'] != m['cohort_id']
            or b['problem_id'] != m['problem_id'] or b['arm_id'] != ARM
            or b['contracts_sha256'] != m['contracts_sha256']
            or c['contracts_sha256'] != m['contracts_sha256']
            or s['original_start'] != c['original_start'] or s['deadline'] != c['deadline']
            or sha(s['board']['snapshot'].encode()) != s['board']['sha256']
            or not board_bound(s, b, h['board_cursor'])
            or s['executor']['version'] != h['executor_version']
            or executor_digest(s['executor']) != h['executor_digest']):
        raise DeploymentRefused('not a matching sealed paused production swarm')
    for artifact in b['artifacts']:
        if sha(base64.b64decode(artifact['base64'], validate=True)) != artifact['sha256']:
            raise DeploymentRefused('saved artifact changed')
    ids = [r['id'] for r in b['records']]
    artifacts = {x['sha256'] for x in b['artifacts']}
    if len(ids) != len(set(ids)) or ids != sorted(ids) or any(
            r['artifact'] and r['artifact'] not in artifacts for r in b['records']):
        raise DeploymentRefused('saved board references inconsistent')
    tables = a['tables']; settings = {r['key']: r['value'] for r in tables['settings']}
    for key in ('blocked', 'cohort'):
        settings[key] = json.loads(settings[key])
    if (not str(settings['blocked'].get('reason','')).startswith('controller-handoff')
            or settings.get('operator-paused') != 'true' or tables['lookup_locks'] or tables['waiters']
            or any(r['slot'] or r['reservation'] or r['lookup_active']
                   or r['state'] not in ('settled', 'unresolved') for r in tables['attempts'])
            or any(r['pause'] for r in tables['arms'])
            or any(r['kind'] == 'accounting-anomaly' and not (failed_resume(data) and resolved_anomaly(
                r, {x['id']: x for x in tables['attempts']})) for r in tables['events'])):
        raise DeploymentRefused('unsafe or unrelated accounting fence')
    registration = settings['cohort']
    if (registration['id'] != m['cohort_id'] or registration['problem'] != m['problem_id']
            or registration['cap'] != m['cap_microusd']):
        raise DeploymentRefused('accounting identity differs')
    # Frozen at the pause: the published excluded time is the sealed excluded
    # time plus the pause so far, observed when the status was written.
    if (clock['since'] is None or clock['since'] != st['paused_since']
            or abs(clock['excluded'] + st['observed_at'] - clock['since'] - st['excluded_seconds']) > 1e-3):
        raise DeploymentRefused('scientific clock mismatch')
    if sha(data['problem'].encode()) != m['problem']['sha256']:
        raise DeploymentRefused('problem changed')
    return data


def load_paused(manifest, job, *, failed_resume=False):
    """Sealed paused state of an ended allocation; nothing original is written."""
    from .cohort_restore import digest
    m = json.loads(Path(manifest).read_bytes()); root = Path(m['durable_root']).resolve(strict=True)
    ctl = root/'operator-control'
    control = json.loads((ctl/'resume.json').read_bytes())
    hfile = ctl/('resume-record-'+control['handoff']+'.json')
    envelope = json.loads(hfile.read_bytes()); record = envelope['record']
    sealed = record['swarms'][ARM]
    sfile = latest(root, 'swarm-'+ARM+'-')
    extra = {}
    if failed_resume:
        events = [json.loads(x) for x in (ctl/'events.jsonl').read_text().splitlines() if x.strip()]
        sealed_at = hfile.stat().st_mtime
        with os.scandir(root/'journal') as rows:
            after = sorted(x.name for x in rows if x.stat().st_mtime > sealed_at)
        extra = dict(failed_resume=dict(last_event={k: events[-1].get(k) for k in ('event', 'handoff')},
                                        journal_after_seal=after, sealed_mtime=sealed_at))
    data = dict(schema=1, source_kind='sealed-pause',
                **({} if failed_resume else dict(scientific_freeze=False)),
                original_status='failed-same-job-resume-of-sealed-pause' if failed_resume else 'allocation-ended-while-paused',
                manifest=m, scheduler=scheduler(job), source_root=str(root),
                status=json.loads((ctl/'status.json').read_bytes()),
                result=json.loads((root/'operational-result.json').read_bytes()) if failed_resume else None, **extra,
                handoff=dict(sha256=control['handoff'], status=envelope['status'], envelope_sha256=envelope['sha256'],
                             record_digest=digest(record), control_ack=control['ack'], record_root=record['root'],
                             record_durable=record['durable'], record_cohort=record['cohort'],
                             board_cursor=sealed['properties']['board_cursor'],
                             executor_version=sealed['executor']['version'],
                             executor_digest=executor_digest(sealed['executor'])),
                swarm=read_publication(sfile),
                checkpoint=dict(controller=record['state'], accounting=record['accounting']),
                **({} if not failed_resume else dict(executor_source='seal', scientific_freeze=False)),
                problem=Path(m['problem']['source']).read_text(),
                namespace=m['local_root']+'/arms/'+ARM,
                selected_publications=[str(sfile), str(hfile)])
    if failed_resume:
        # The seal is the newer executor state (P10: one more handled result than
        # the last swarm publication at the same version and board cursor).
        published = data['swarm']['executor']
        data['failed_resume']['published_executor_digest'] = executor_digest(published)
        if published['version'] == sealed['executor']['version'] and published['assignments'] == sealed['executor']['assignments']:
            data['swarm']['executor'] = copy.deepcopy(sealed['executor'])
    validate_saved(data)
    with tempfile.TemporaryDirectory(prefix='closing-frozen-audit-', dir='/tmp') as d:
        store = AccountingStore.restore_checkpoint(Path(d)/'accounting.sqlite', data['checkpoint']['accounting'],
                    fence_proof='terminal-scheduler:'+str(job))
        store.assert_conservation()
        data['prior_totals'] = store.totals()
    return data


def load_saved(manifest, job, *, salvaged=False):
    m = json.loads(Path(manifest).read_bytes()); root = Path(m['durable_root']).resolve(strict=True)
    sfile = latest(root, 'swarm-'+ARM+'-'); cfile = latest(root, 'cohort-checkpoint-')
    data = dict(schema=1, manifest=m, scheduler=scheduler(job), source_root=str(root),
                status=json.loads((root/'operator-control/status.json').read_bytes()),
                result=json.loads((root/'operational-result.json').read_bytes()),
                swarm=read_publication(sfile), checkpoint=read_publication(cfile),
                problem=Path(m['problem']['source']).read_text(),
                namespace=m['local_root']+'/arms/'+ARM,
                selected_publications=[str(sfile), str(cfile)])
    if salvaged:
        data.update(source_kind='salvaged-terminal', scientific_freeze=True,
                    original_status='cancelled-frozen-with-retained-pause-barrier',
                    salvage=json.loads((root/'cancel-salvage.json').read_bytes()))
    if data['result']['status'] != 'failed':
        data['closure'] = json.loads((root/ARM/'closure/terminal/closure/closure.json').read_bytes())
    validate_saved(data)
    # Restoration into disposable local storage audits the complete accounting
    # schema/conservation. No provider lookup, original database or sender touched.
    with tempfile.TemporaryDirectory(prefix='closing-frozen-audit-', dir='/tmp') as d:
        store = AccountingStore.restore_checkpoint(Path(d)/'accounting.sqlite', data['checkpoint']['accounting'],
                    fence_proof='terminal-scheduler:'+str(job))
        store.assert_conservation()
        data['prior_totals'] = store.totals()
    return data


class ReportAccounting(AccountingStore):
    # Original attempts are audit evidence, including unknown bills. Report
    # recovery does not perform a dead-run disposition on that evidence.
    def claim_lookup(self, *, credential=None):
        return super().claim_lookup(credential=credential, exclude=self.retained_attempts)
    def release_ungenerated(self, *, min_age=900):
        return super().release_ungenerated(min_age=min_age, exclude=self.retained_attempts)
    def settle_provisional(self, *, exclude=()):
        return super().settle_provisional(exclude=set(exclude) | set(self.retained_attempts))


def _scheduler_neutral(data):
    """Saved evidence without the scheduler's elapsed-time fields. Closings before the ElapsedRaw
    column (P01, P02, P07 on 2026-10-13) recorded 4-column sacct rows and no elapsed_seconds; the
    terminal-state evidence those rows carry is still compared exactly."""
    data = copy.deepcopy(data)
    sched = data.get('scheduler') if isinstance(data, dict) else None
    if isinstance(sched, dict):
        sched.pop('elapsed_seconds', None)
        if isinstance(sched.get('rows'), list):
            sched['rows'] = [list(r[:4]) if isinstance(r, list) else r for r in sched['rows']]
    return data


def load_prior(data, out, expect_sha256, job, *, check_scheduler=True):
    """A failed report recovery of this same snapshot: its final ledger is the new baseline.

    Its report attempts and holds carry forward, so no money is lost or counted
    twice. Original attempts must still equal their native fenced restore.
    """
    out = Path(out); raw = (out/'accounting-final.json').read_bytes()
    if sha(raw) != expect_sha256:
        raise DeploymentRefused('prior report ledger differs from reviewed digest')
    if _scheduler_neutral(json.loads((out/'recovery-source.json').read_bytes())) != _scheduler_neutral(data):
        raise DeploymentRefused('prior report recovery used different saved evidence')
    ownership = scheduler(job) if check_scheduler else None
    checkpoint = json.loads(raw); tables = checkpoint['tables']
    settings = {r['key']: r['value'] for r in tables['settings']}
    marks = [json.loads(e['payload']) for e in tables['events'] if e['kind'] == 'frozen-report-recovery']
    if (json.loads(settings.get('blocked', 'null')) != {'reason': 'frozen-report-recovery-shutdown'}
            or not marks or any(m.get('original_job') != data['scheduler']['job']
                or m.get('board_sha256') != data['swarm']['board']['sha256'] for m in marks)
            or len(marks) > 1 and marks[-1].get('prior_report_job') is None
            or next(r for r in tables['arms'] if r['id'] == ARM)['mode'] not in
                (('report', 'terminal') if (out/'report-result.json').exists() else ('report',))):
        raise DeploymentRefused('prior ledger is not a shut-down report recovery of this run')
    if (out/'report-clock.json').exists():
        report_clock = json.loads((out/'report-clock.json').read_bytes())
    else:
        # A native failure during pinning can have final ledger/duration but no
        # runtime clock yet. Admit only a labelled pre-provider episode with an
        # exact prior ledger and no new attempts or changes to their money.
        label = json.loads((out/'proof-replay-label.json').read_bytes())
        mark = marks[-1]
        matches = [Path(p) for p, h in label.get('original_files', {}).items()
                   if Path(p).name == 'accounting-final.json' and h == mark.get('prior_ledger_sha256')]
        if (label.get('owner') != 'ds41-proof-replay' or len(matches) != 1 or
                label.get('snapshot_sha256') != sha(json.dumps(data, sort_keys=True).encode()) or
                label.get('prior_job') != mark.get('prior_report_job') or
                label.get('prior_ledger_sha256') != mark.get('prior_ledger_sha256') or
                matches[0].parent.resolve() == out.resolve() or
                any((out/n).exists() for n in ('journal', 'mixed-swarm', 'report-result.json'))):
            raise DeploymentRefused('missing clock is not a bound pre-provider salvage')
        previous = load_prior(data, matches[0].parent, mark['prior_ledger_sha256'],
                              mark['prior_report_job'], check_scheduler=check_scheduler)
        old = {r['id']: r for r in previous['checkpoint']['tables']['attempts']}
        now = {r['id']: r for r in tables['attempts']}
        # Native restore may clear an inactive slot; it cannot add a request,
        # alter a generation/receipt/outcome, or change any amount here.
        if (old.keys() != now.keys() or label.get('prior_report_seconds') != previous['consumed_seconds'] or
                any({k:v for k,v in old[i].items() if k != 'slot'} !=
                    {k:v for k,v in now[i].items() if k != 'slot'} or
                    now[i].get('slot') not in (None, 0, old[i].get('slot')) for i in old)):
            raise DeploymentRefused('pre-provider salvage changed attempts or lost prior time')
        report_clock = dict(previous_report_attempt=dict(consumed_seconds=previous['consumed_seconds']))
    earlier = (report_clock.get('previous_report_attempt') or {}).get('consumed_seconds', 0.)
    if (len(marks) > 1) != bool(report_clock.get('previous_report_attempt')):
        raise DeploymentRefused('prior report attempt chain is inconsistent')
    receipt_path = out/'report-duration.json'
    if receipt_path.exists():
        duration = json.loads(receipt_path.read_bytes())
        if (duration.get('schema') != 1 or duration.get('job') != str(job) or
                duration.get('accounting_sha256') != expect_sha256):
            raise DeploymentRefused('report duration is not bound to prior job and ledger')
        seconds = duration['elapsed_seconds']
        clock_source = str(receipt_path)
    elif ownership is not None:
        # Historical attempts had no duration receipt. Scheduler allocation time
        # counts their no-call and setup time too, instead of silently losing it.
        seconds = ownership['elapsed_seconds']
        clock_source = 'sacct:ElapsedRaw:' + str(job)
    else:
        raise DeploymentRefused('prior elapsed time requires duration receipt or scheduler evidence')
    if any(type(x) not in (int,float) or not math.isfinite(x) or x < 0 for x in (seconds,earlier)):
        raise DeploymentRefused('invalid measured report duration')
    return dict(job=str(job), out=str(out), sha256=expect_sha256, checkpoint=checkpoint,
                consumed_seconds=earlier+seconds, clock_source=clock_source)



def recovery_ledger(path, data, clock, prior=None):
    """The caller owns new storage. Only report admission is enabled in this copy."""
    validate_saved(data)
    # With a prior attempt, the original ledger is restored beside the new one
    # only to prove that every original attempt row carried forward unchanged.
    native = ReportAccounting.restore_checkpoint(path if prior is None else Path(str(path)+'.original-audit'),
                data['checkpoint']['accounting'], fence_proof='terminal-scheduler:'+data['scheduler']['job'],
                clock=clock, lookup_clock=time.time)
    store = native
    if prior is not None:
        expected = {r['id']: r for r in native.export()['attempts']}
        store = ReportAccounting.restore_checkpoint(path, prior['checkpoint'],
                    fence_proof='terminal-scheduler:'+prior['job'], clock=clock, lookup_clock=time.time)
        rows = {r['id']: r for r in store.export()['attempts']}
        if any(rows.get(k) != v for k, v in expected.items()):
            raise DeploymentRefused('prior report ledger changed original attempts')
        original_anomalies = [e for e in native.export()['events'] if e['kind'] == 'accounting-anomaly']
        # A failed sealed resume may already contain an estimate-over-bound
        # event that the source validator proved settled within its bound.
        # Retain that exact event; do not treat it as a new prior-report fault.
        if any(e['kind'] == 'accounting-anomaly' and not (
                failed_resume(data) and e in original_anomalies and resolved_anomaly(e, rows))
                for e in store.export()['events']):
            raise DeploymentRefused('prior report ledger has an accounting anomaly')
        # The dead attempt's own send gate paused the report arm (live 1842256/1842580:
        # a carried 'durability-unacknowledged' pause admitted no call for the whole rerun).
        # Its requests are fenced and held above; only those two pauses are cleared.
        # The fenced restore overwrites the pause; judge the prior ledger's own value.
        carried = next(r for r in prior['checkpoint']['tables']['arms'] if r['id'] == ARM)['pause']
        if carried not in (None, 'durability-unacknowledged', 'ambiguous-restored-request'):
            raise DeploymentRefused('prior report ledger arm paused: '+str(carried))
        with store.transaction() as db:
            pause = db.execute('SELECT pause FROM arms WHERE id=?', (ARM,)).fetchone()['pause']
            if pause not in (None, carried, 'ambiguous-restored-request'):
                raise DeploymentRefused('restored report arm paused: '+str(pause))
            db.execute('UPDATE arms SET pause=NULL WHERE id=?', (ARM,))
    before = store.export()['attempts']
    store.retained_attempts = frozenset(r['id'] for r in before)
    with store.transaction() as db:
        db.execute("DELETE FROM settings WHERE key='blocked'")
        # A sealed pause also carries the operator barrier; only this copy reports.
        db.execute("DELETE FROM settings WHERE key='operator-paused'")
        db.execute("UPDATE arms SET mode='report' WHERE id=?", (ARM,))
        # Dead workers own no live clients. Holds are separate and remain intact.
        db.execute("DELETE FROM settings WHERE key='client-slots'")
        store.event(db, 'frozen-report-recovery', dict(original_job=data['scheduler']['job'],
                board_sha256=data['swarm']['board']['sha256'], original_status=data.get('original_status',(data.get('result') or {}).get('status','failed')),
                original_clock=data['checkpoint']['controller']['operator_clock'],
                scientific_freeze=data.get('scientific_freeze', True),
                **({} if prior is None else dict(prior_report_job=prior['job'], prior_ledger_sha256=prior['sha256']))))
    if store.export()['attempts'] != before:
        raise DeploymentRefused('recovery changed old attempts')
    store.assert_conservation()
    return store


class FrozenBoard:
    """No board RPC, capability creation, posts or state mutation in report recovery."""
    def __init__(self, data):
        self.saved = copy.deepcopy(data['swarm']['board'])
        self.swarm = data['swarm']
    def export_snapshot(self, secret):
        return copy.deepcopy(self.saved)
    def freeze(self, secret):
        return self.export_snapshot(secret)
    def enter_report_mode(self, secret):
        return None
    def ledger(self, secret, policy):
        from .claim_import import session_ledger
        from .board import MODEL_KEYS
        bound = {k:r['bound_record'] for k,r in self.swarm['executor']['assignments'].items()
                 if r.get('bound_record') is not None}
        return session_ledger(json.loads(self.saved['snapshot'])['records'], policy,
                    {k:MODEL_KEYS.get(v,v) for k,v in self.swarm['workers'].items()}, bound)
    def head(self, secret):
        records = json.loads(self.saved['snapshot'])['records']
        return max((x['id'] for x in records), default=0), len(records)
    def __getattr__(self, name):
        raise DeploymentRefused('frozen report recovery forbids board operation: '+name)


def runtime_class():
    from .swarm_runtime import SwarmRuntime
    class FrozenClosing(SwarmRuntime):
        def _bound_policy(self, policy=None):
            # Saved review bindings stay authoritative; new report audits do not
            # confer scientific certification or replace the old role roster.
            pol = super()._bound_policy(policy)
            if hasattr(self, 'saved_assignments'):
                pol['bound_reviews'] = {k:r['bound_record'] for k,r in self.saved_assignments.items()
                                       if r.get('bound_record') is not None}
                pol['pending_reviews'] = {}
            return pol
        def _publication_manifest(self):
            from .claim_manifest import manifest
            from .claim_ledger import load_policy
            return manifest(self.claim_ledger(), load_policy(self.strategy), self.claim_namespace)
        def _publish(self, *, identity, kind, body):
            atomic_json(self.path/('administrative-'+sha(identity.encode())+'.json'),
                        dict(identity=identity, kind=kind, body=body))
        def close(self, reason='controller-stop'):
            super().close(reason)
            self.accounting.control('frozen-report-recovery-terminal', self.strategy, mode='terminal')
        def step(self):
            # The native writer, backup, compiler, blinded semantic reviewers and
            # bounded revision stages remain. There is no orchestration/research.
            with self.mutex:
                self.publisher.poll(); self.reconciliation_gateway.reconcile(); self.watchdog.closure_poll()
                self.flush_checkpoint()
                self.executor.tick(research_available=0, min_request_bound=1)
                state = self.executor.snapshot(); self._drive_closing(state)
                if self.mode == 'terminal':
                    self._finish_terminal(); return
                for w in list(self.slots.values()):
                    if w.worker: w.consume()
                    if w.worker and not w.worker.alive(): w.state['slot_failure']='worker-exited'
                    closed = self.clock() >= w.stage_deadline or not self.role_allowed(w)
                    if w.state.get('turn_complete') or w.state.get('slot_failure') or w.state.get('admission_stop') or closed:
                        self._finish_slot(w, forced=closed)
                w = self.watchdog
                if w.worker:
                    w.report_gate_on=self._report_gate_on(); w.consume(); w.promote_report(w.worker)
                    if w.state.get('turn_complete') or w.state.get('admission_stop') or w.state.get('slot_failure'):
                        if self._reap(w) and not w.state.get('build_pending'):
                            if not w.report_cap_failure() and not self._report_retry(state,w):
                                w.request_build('writer-turn-complete')
                self._drive_backup()
                for slot in self.slots.values():
                    if slot.context.role not in ('verify','report'):
                        raise DeploymentRefused('non-report slot in frozen closing')
                    if not slot.worker and self.publisher.require_ack(slot.publication): self._launch(slot)
    return FrozenClosing


def run_saved(data, root, out, source, *, fake=None, clock=None, observer=None, prior=None, deliver_by=None):
    from .deployment import require_compute
    from .contracts_v3 import load_contracts
    from .durability import Publisher
    from .pilot_job import refresh_routes, credential_loader, StopRequest
    from .release import pin_release, verify_dependency_pin
    import signal
    elapsed_start = time.monotonic()
    validate_saved(data)
    if fake is None:
        require_compute()
        if observer is not None or clock is not None: raise DeploymentRefused('live report clock/observer override refused')
        if int(os.environ.get('SLURM_MEM_PER_NODE','0'))<65536 or int(os.environ.get('SLURM_CPUS_PER_TASK','0'))<16:
            raise DeploymentRefused('frozen closing requires64GiB and16CPUs')
    root, out, source = Path(root), Path(out), Path(source)
    original = Path(data['source_root'])
    if (out.resolve().is_relative_to(original.parent) or original.is_relative_to(out.resolve())
            or root.resolve().is_relative_to(original.parent)
            or root.resolve().is_relative_to(Path(data['manifest']['local_root']))
            or out.resolve().is_relative_to(Path(data['manifest']['local_root']))):
        raise DeploymentRefused('separate report recovery output required')
    if root.exists() or out.exists(): raise DeploymentRefused('new unique recovery storage required')
    root.parent.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700); out.mkdir(mode=0o700)
    # Atomic new output is also a one-shot receipt: failure is evidence, never an
    # invitation to rerun against an old output or renew its closing schedule.
    atomic_json(out/'recovery-source.json', data)
    os.environ['V3_POLICY_OVERRIDES']=json.dumps(data['manifest'].get('policy_overrides',{}))
    os.environ.pop('V3_PRODUCTION_CONTINUATION', None)
    os.environ.pop('V3_SEED_MIXED_SWARM', None)
    c = data['checkpoint']['controller']
    # Terminal: active time stopped at the final status. Paused: it stopped at the pause.
    stopped = c['operator_clock']['since'] if data.get('source_kind')=='sealed-pause' else (data['failed_owner']['ended_at'] if data.get('source_kind')=='failed-unfrozen' else data['status']['observed_at'])
    clock = clock or RunClock(excluded=c['operator_clock']['excluded'], since=stopped)
    # A failed report attempt's reporting seconds stay counted against the run.
    used = 0. if prior is None else prior['consumed_seconds']
    if clock() + used + 10140 + 420 >= c['cleanup_deadline']:
        raise DeploymentRefused('original remaining clock cannot fit report recovery')
    store = recovery_ledger(root/'accounting.sqlite', data, clock, prior)
    baseline = store.totals()
    publisher = runtime = None
    stop = StopRequest(); old = signal.signal(signal.SIGTERM, stop)
    try:
        # Bind time before dependency pinning/credentials so a preflight crash
        # remains natively chainable with every earlier episode still counted.
        atomic_json(out/'report-clock.json', dict(phase='pre-provider-preflight',
            original_start=c['original_start'], original_deadline=c['deadline'],
            original_clock=c['operator_clock'], previous_report_attempt=None if prior is None else
            dict(job=prior['job'], out=prior['out'], ledger_sha256=prior['sha256'],
                 consumed_seconds=prior['consumed_seconds'])))
        policy = load_contracts(expected_sha256=data['manifest']['contracts_sha256'])
        if fake is None:
            prices, routes = refresh_routes(policy, 'production')
            atomic_json(out/'route-preflight.json', routes)
            release = pin_release(source, root)
            verify_dependency_pin(release, data['manifest']['runtime_pin'])
            # Actual namespace/tool preflight, still before credential access.
            from types import SimpleNamespace
            from .cohort_readiness import prepare
            publisher = Publisher(root/'publisher', out/'journal')
            check = SimpleNamespace(root=root, policy=policy, release=release,
                deployment=dict(mode='production', fixtures=[], problem=data['manifest']['problem']),
                live=True, streams={ARM:None}, publisher=publisher, problem=data['manifest']['problem_id'],
                cohort=data['manifest']['cohort_id'], profile='production', durable=out, clock=clock)
            prepare(check)
            key = credential_loader(data['manifest']['credential_file'])()
        else:
            prices = {x['model_id']:['.0000003','.0000012'] for x in policy['models'].values()}
            release = pin_release(source, root); key = None
            publisher = Publisher(root/'publisher', out/'journal')
        if type(clock) is RunClock: clock.resume()
        runtime = runtime_class()(root/'arms', strategy=ARM, profile='production',
            cohort=data['manifest']['cohort_id'], problem=data['manifest']['problem_id'],
            start=c['original_start'], contracts_sha256=data['manifest']['contracts_sha256'],
            release=release, accounting=store, publisher=publisher, durable_root=out, prices=prices,
            fake=fake, clock=clock, deployment=(dict(mode='production',fixtures=[],problem=data['manifest']['problem'])
                if fake is None else dict(mode='test',fixtures=[])), key=key,
            credential_id='production-'+data['manifest']['cohort_id'], prices_verified=fake is None,
            metadata_lookup_free=True)
        runtime.stop_services()  # base constructor's unused new board RPC only
        runtime.closed=False
        runtime.board = FrozenBoard(data); runtime.claim_namespace=data['namespace']
        runtime.saved_assignments=data['swarm']['executor']['assignments']
        runtime.mode='report'; runtime.started=True
        runtime.watchdog.work.joinpath('PROBLEM.md').write_text(data['problem'])
        runtime.watchdog.work.joinpath('SOLUTION-TEMPLATE.tex').write_bytes((source/'template/SOLUTION-TEMPLATE.tex').read_bytes())
        runtime.executor.begin_closing('terminal-frozen-report-recovery')
        runtime.checkpoint()
        atomic_json(out/'report-clock.json', dict(original_start=c['original_start'],
            original_deadline=c['deadline'], original_clock=c['operator_clock'], reporting_clock=clock.snapshot() if type(clock) is RunClock else None,
            recovery_closing=runtime.executor.snapshot()['closing'],
            previous_closing=data['swarm']['executor']['closing'],
            source_kind=data.get('source_kind','terminal-frozen'), scientific_freeze=data.get('scientific_freeze', True),
            previous_report_attempt=None if prior is None else dict(job=prior['job'], out=prior['out'],
                ledger_sha256=prior['sha256'], consumed_seconds=prior['consumed_seconds'])))
        while not runtime.watchdog.state.get('terminal'):
            if deliver_by is not None and time.time() >= deliver_by:
                raise DeploymentRefused('operator deliver-by reached')
            stop.check(); runtime.step()
            if observer is not None: observer(runtime)
            time.sleep(.05)
        # Native metadata owners finish first. A bounded final pass settles only
        # NEW report attempts with positive provider metadata; old liabilities
        # are retained byte-for-byte, and missing/zero metadata remains held.
        runtime.reconciliation_gateway.wait_reconciliation(seconds=330)
        from .cancel_salvage import settle_unresolved
        from decimal import Decimal
        class NewAttempts:
            def export(self):
                return dict(attempts=[r for r in store.export()['attempts'] if r['id'] not in store.retained_attempts])
            def settle(self, rid, cost): return store.settle(rid, cost)
        def positive_lookup(generation):
            value=runtime.reconciliation_gateway.lookup(generation)
            if isinstance(value,dict) and value.get('total_cost') is not None and Decimal(str(value['total_cost']))>0:
                return value
            return None
        settlement=settle_unresolved(NewAttempts(),positive_lookup,seconds=120,zero_ungenerated=False)
        store.assert_conservation()
        atomic_json(out/'report-result.json', dict(final_report_settlement=settlement,original_job=data['scheduler']['job'],
            original_status=data.get('original_status',(data.get('result') or {}).get('status','failed')), board_sha256=data['swarm']['board']['sha256'],
            closure=runtime.watchdog.state['closure_manifest'], prior_totals=baseline,
            original_prior_totals=data['prior_totals'],
            previous_report_attempt=None if prior is None else dict(job=prior['job'], ledger_sha256=prior['sha256']),
            totals=store.totals(), original_clock=c['operator_clock']))
    except BaseException:
        # Preserve a draft even if closure/catalog/publisher cleanup crashes.
        # This separate delivery receipt does not manufacture a scientific fence.
        if runtime is not None:
            try:
                from .deadline_delivery import release_runtime
                release_runtime(runtime,out,data['manifest']['problem_id'].removeprefix('b3-'),
                                reason='closing-crashed; checks failed or not reached')
            except Exception as exc:
                atomic_json(out/'deadline-delivery-error.json',dict(error=type(exc).__name__+': '+str(exc)[:500]))
        raise
    finally:
        store.block('frozen-report-recovery-shutdown')
        shutdown_errors=[]
        if runtime is not None:
            for w in [runtime.watchdog, *runtime.slots.values(), *([runtime.backup] if getattr(runtime,'backup',None) else [])]:
                try:
                    if w.worker:w.stop_worker()
                except Exception as exc:
                    shutdown_errors.append(type(exc).__name__+': '+str(exc)[:300])
                    if w.worker is not None:
                        try:w.worker.stop()
                        except Exception as stop_error:shutdown_errors.append(str(stop_error)[:300])
            for gateway in runtime.gateways:
                try:gateway.cancel_worker(None)
                except Exception as exc:shutdown_errors.append(str(exc)[:300])
        if shutdown_errors:atomic_json(out/'closing-shutdown-errors.json',dict(errors=shutdown_errors[:30]))
        atomic_json(out/'accounting-final.json', store.checkpoint())
        atomic_json(out/'report-duration.json', dict(schema=1, job=os.environ.get('SLURM_JOB_ID','synthetic'),
            elapsed_seconds=time.monotonic()-elapsed_start,
            accounting_sha256=sha((out/'accounting-final.json').read_bytes())))
        if publisher is not None: publisher.close()
        signal.signal(signal.SIGTERM, old)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True); p.add_argument('--job', required=True)
    p.add_argument('--snapshot'); p.add_argument('--run', action='store_true')
    p.add_argument('--expect-sha256'); p.add_argument('--root'); p.add_argument('--out')
    p.add_argument('--paused', action='store_true', help='source is a sealed pause of an ended allocation')
    p.add_argument('--prior-out'); p.add_argument('--prior-sha256'); p.add_argument('--prior-job')
    a=p.parse_args(argv)
    data=(load_paused if a.paused else load_saved)(a.manifest,a.job)
    raw=json.dumps(data,sort_keys=True).encode(); digest=sha(raw)
    if a.run:
        if not a.snapshot or not a.expect_sha256 or not a.root or not a.out:
            p.error('run requires --snapshot --expect-sha256 --root --out')
        saved=Path(a.snapshot).read_bytes()
        if sha(saved)!=a.expect_sha256 or saved!=raw:
            raise DeploymentRefused('reviewed recovery snapshot differs from saved terminal evidence')
        from .deployment import require_compute
        require_compute()
        prior = None
        if a.prior_out or a.prior_sha256 or a.prior_job:
            if not (a.prior_out and a.prior_sha256 and a.prior_job):
                p.error('a prior report attempt needs --prior-out --prior-sha256 --prior-job')
            prior = load_prior(data, a.prior_out, a.prior_sha256, a.prior_job)
        # One report job per reviewed source snapshot, even if alternate outputs
        # are supplied accidentally. Original durable roots remain untouched.
        with Path(str(a.snapshot)+'.closing-claim').open('x') as claim:
            claim.write(json.dumps(dict(job=os.environ['SLURM_JOB_ID'], snapshot_sha256=a.expect_sha256)))
        run_saved(data,a.root,a.out,Path(__file__).resolve().parents[1],prior=prior)
    elif a.snapshot:
        path=Path(a.snapshot)
        with path.open('xb') as f: f.write(raw)
        path.chmod(0o600)
    if not a.run and a.prior_out:
        prior = load_prior(data, a.prior_out, a.prior_sha256, a.prior_job)
        with tempfile.TemporaryDirectory(prefix='closing-frozen-prior-', dir='/tmp') as d:
            store = recovery_ledger(Path(d)/'accounting.sqlite', data, time.time, prior)
            baseline = store.totals()
            arm = next(r for r in store.export()['arms'] if r['id'] == ARM)
        print(json.dumps(dict(prior_job=prior['job'], prior_ledger_sha256=prior['sha256'],
            prior_consumed_seconds=prior['consumed_seconds'], baseline_totals=baseline,
            report_arm=dict(mode=arm['mode'], pause=arm['pause']))))
    print(json.dumps(dict(dry_run=not a.run, snapshot_sha256=digest,
        board_sha256=data['swarm']['board']['sha256'], prior_totals=data['prior_totals'],
        original_job=a.job, original_status=data.get('original_status',(data.get('result') or {}).get('status','failed')), source_mutated=False,
        source_kind=data.get('source_kind','terminal-frozen'))))

if __name__=='__main__': main()
