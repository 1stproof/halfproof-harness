"""Cohort-local accounting authority; dollars are integer microdollars.

Keep this database on local storage, under one cohort controller. SQLite serializes
racing handlers. A paid sender must obtain a durable external acknowledgment of
its reservation before ``mark_sent``. Workers never choose accounts or routes.
No automatic expiration proves a request unbilled or an upstream slot released.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import threading
import uuid

LOOKUP_DELAYS = (60, 300, 1200, 3600, 14400, 28800, 82800)
LOOKUP_HORIZON = 86400
# Live Siebert run (2026-10-15): OpenRouter's generation endpoint answered 404 for nearly
# every request for over 30 minutes; settled spend read $0.0027 against $9.71 of streamed
# estimates, so the settled-spend research stop could not fire. A finished reply whose cost
# record has not arrived this long after it ended settles provisionally at its streamed usage
# estimate (usage cost plus counted search fees). Lookups continue on the same seven-lookup
# schedule and a real record replaces the provisional amount. No estimate: liability stays.
PROVISIONAL_AFTER = 600
PROVISIONAL = 'provisional:'
from contracts.route import routes as _contract_routes
OSS_ROUTES = dict(_contract_routes().values())

CHECKPOINT_KEYS = dict(settings='key', arms='id', accounts='id', routes='id', attempts='id',
                       waiters='id', events='seq', controls='id', lookup_locks='credential', grants='id')

ROLES = {'research', 'review', 'verify', 'report', 'recovery', 'specialist', 'orchestrator'}


class UnreapedOperation(RuntimeError):
    """A killed child did not exit; preserve its exclusive service lock."""


class AdmissionDenied(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def integer(value, *, positive=False):
    if type(value) is not int or value < int(positive):
        raise ValueError('expected nonnegative integer microdollars/count')
    return value


def micros(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError('invalid cost')
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class AdmissionContext:
    """Controller-created identity; never populated from an HTTP request body."""
    def __post_init__(self):
        if self.max_output_tokens == 0:
            from .output_limits import provider_max
            object.__setattr__(self, 'max_output_tokens', provider_max(self.model))
    arm: str
    account: str
    model: str
    provider: str
    credential: str  # opaque credential identifier, never the credential itself
    role: str = 'research'
    request_class: str = 'primary'
    max_input_tokens: int = 196_608
    max_output_tokens: int = 0  # 0: the model's provider maximum (output_limits)
    tool_bound: int = 0
    search_provenance: str | None = None
    cohort: str = 'unregistered'
    problem: str = 'unregistered'
    worker: str = 'unregistered'
    phase: str = 'research'
    ticket: str | None = None
    logical_request: str | None = None


class AccountingStore:
    _locks = {}
    _locks_guard = threading.Lock()

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lookup_clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS arms(id TEXT PRIMARY KEY, config TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'research', pause TEXT);
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, parent TEXT,
                arm TEXT, cap INTEGER NOT NULL, purpose TEXT NOT NULL, model TEXT,
                deadline REAL NOT NULL, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, arm TEXT NOT NULL,
                account TEXT NOT NULL, context TEXT NOT NULL, bound INTEGER NOT NULL,
                state TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0,
                liability INTEGER NOT NULL DEFAULT 0, reservation INTEGER NOT NULL,
                released INTEGER NOT NULL DEFAULT 0, overrun INTEGER NOT NULL DEFAULT 0,
                estimate INTEGER, generation TEXT, created REAL NOT NULL, ended REAL,
                deadline REAL NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                slot INTEGER NOT NULL DEFAULT 1, lookups INTEGER NOT NULL DEFAULT 0,
                lookup_active INTEGER NOT NULL DEFAULT 0, lookup_token TEXT,
                outcome TEXT, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS waiters(id TEXT PRIMARY KEY, route TEXT NOT NULL,
                arm TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY, arm TEXT NOT NULL,
                account TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lookup_locks(credential TEXT PRIMARY KEY,
                attempt TEXT NOT NULL, token TEXT NOT NULL);
            ''')
            if 'priority' not in {r[1] for r in db.execute('PRAGMA table_info(waiters)')}:
                db.execute('ALTER TABLE waiters ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
            # Transactional change capture: a committed reservation and every later
            # settlement/control/delete enter the same durable journal transaction.
            db.execute('CREATE TABLE IF NOT EXISTS checkpoint_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, row_key TEXT NOT NULL)')
            for table, key in CHECKPOINT_KEYS.items():
                for action, ref in (('INSERT','NEW'), ('UPDATE','NEW'), ('DELETE','OLD')):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS checkpoint_{table}_{action} AFTER {action} ON {table} BEGIN INSERT INTO checkpoint_changes(table_name,row_key) VALUES ('{table}',CAST({ref}.{key} AS TEXT)); END")

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def transaction(self):
        # Serialize local handler/metadata/controller transactions before
        # SQLite's busy loop. Separate store instances for this same retained
        # database share the lock; a fork gets a new process-specific identity.
        identity=(os.getpid(),str(self.path.resolve()))
        with self._locks_guard:
            lock=self._locks.setdefault(identity,threading.Lock())
        if not lock.acquire(timeout=5):raise sqlite3.OperationalError('local accounting serialization timeout')
        db = None
        try:
            db = self.connect()
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            if db is not None:db.rollback()
            raise
        finally:
            if db is not None:db.close()
            lock.release()

    @staticmethod
    def event(db, kind, payload):
        db.execute('INSERT INTO events(kind,payload) VALUES (?,?)', (kind, canonical(payload)))

    def register_cohort(self, cohort, *, problem, profile, cap, arm_caps, recovery_reserve=0, contracts_sha256, contracts_directory=None):
        integer(cap, positive=True); integer(recovery_reserve)
        for value in arm_caps.values(): integer(value, positive=True)
        if profile not in ('pilot', 'production', 'swarm-check', 'solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue') or not arm_caps or sum(arm_caps.values()) + recovery_reserve != cap:
            raise ValueError('cohort partition does not conserve cap')
        if profile != 'pilot' and recovery_reserve: raise ValueError('production has no extra recovery grant')
        from .contracts_v3 import load_contracts
        directory=contracts_directory or (Path(__file__).resolve().parent/'contracts' if (Path(__file__).resolve().parent/'contracts').is_dir() else None)
        policy=load_contracts(directory,expected_sha256=contracts_sha256)
        expected={arm:row['cap_microusd'] for arm,row in policy[profile].items()}
        if arm_caps!=expected or cap!=policy[profile+'_total_microusd'] or recovery_reserve!=(policy['pilot_shared_recovery_microusd'] if profile=='pilot' else 0):
            raise AdmissionDenied('fixed-strategy-allocation')
        config = canonical(dict(id=cohort, problem=problem, profile=profile, cap=cap,
                                arm_caps=arm_caps, reserve=recovery_reserve, contracts_sha256=contracts_sha256))
        with self.transaction() as db:
            old = db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()
            if old and old[0] != config: raise AdmissionDenied('immutable-cohort')
            for row in db.execute('SELECT id,config FROM arms'):
                if arm_caps.get(row['id']) != json.loads(row['config'])['cap']:
                    raise AdmissionDenied('cohort-allocation')
            db.execute("INSERT OR IGNORE INTO settings VALUES ('cohort',?)", (config,))

    @staticmethod
    def _cap(db, account):
        grants = db.execute('SELECT COALESCE(SUM(amount),0) FROM grants WHERE arm=?', (account['id'],)).fetchone()[0]
        return account['cap'] + grants

    def grant_recovery(self, grant_id, *, arm, amount, reason):
        """Infrastructure contingency cannot become inference money for any arm.

        Keep the former entry point as an explicit denial for old callers. Prior
        ledger records remain auditable; no new grant/account/control is written.
        """
        integer(amount, positive=True)
        raise AdmissionDenied('original-total-no-topups-infrastructure-contingency')

    def arm_config(self, arm):
        with self.transaction() as db:
            row=db.execute('SELECT config FROM arms WHERE id=?',(arm,)).fetchone()
            if not row:raise ValueError('unknown arm')
            return json.loads(row[0])

    def effective_cap(self, arm):
        """Original cap plus an explicitly debited pilot grant, never a reset."""
        with self.transaction() as db:
            row=db.execute('SELECT * FROM accounts WHERE id=?',(arm,)).fetchone()
            if not row:raise ValueError('unknown arm')
            return self._cap(db,row)

    def register_arm(self, arm, *, cap, start, deadline, models, solo=False):
        integer(cap, positive=True)
        if not math.isfinite(start) or not math.isfinite(deadline) or deadline <= start:
            raise ValueError('invalid immutable wall')
        if not models or any(m not in OSS_ROUTES for m in models):
            raise AdmissionDenied('model-not-allowed')
        config = canonical(dict(cap=cap, start=start, deadline=deadline, models=sorted(models), solo=bool(solo)))
        with self.transaction() as db:
            cohort = db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()
            if cohort and json.loads(cohort[0])['arm_caps'].get(arm) != cap:
                raise AdmissionDenied('cohort-allocation')
            old = db.execute('SELECT config FROM arms WHERE id=?', (arm,)).fetchone()
            if old:
                if old['config'] != config: raise AdmissionDenied('immutable-arm')
                return
            db.execute('INSERT INTO arms(id,config) VALUES (?,?)', (arm, config))
            self._account(db, arm, None, arm, cap, 'arm', None, deadline)
            self.event(db, 'arm', dict(id=arm, config=json.loads(config)))

    def _account(self, db, account, parent, arm, cap, purpose, model, deadline):
        config = canonical(dict(parent=parent, arm=arm, cap=cap, purpose=purpose, model=model, deadline=deadline))
        old = db.execute('SELECT config FROM accounts WHERE id=?', (account,)).fetchone()
        if old:
            if old['config'] != config: raise AdmissionDenied('immutable-account')
            return
        db.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)',
                   (account, parent, arm, cap, purpose, model, deadline, config))

    def register_account(self, account, *, parent, cap, purpose, model=None, deadline=None):
        """Carve fixed subaccounts; sibling allocations cannot exceed their parent.

        Creating children closes direct admission against their parent, preventing
        research from raiding report/review allocations. Ticket caps are carved
        from the same fixed pool, never a new allocation on restart.
        """
        integer(cap, positive=True)
        if purpose not in ROLES | {'model', 'pool', 'ticket', 'dynamic-research', 'closing-writer', 'closing-verifier-a', 'closing-verifier-b', 'closing-control'}:
            raise ValueError('invalid account purpose')
        with self.transaction() as db:
            p = db.execute('SELECT * FROM accounts WHERE id=?', (parent,)).fetchone()
            if not p: raise ValueError('unknown parent')
            deadline = p['deadline'] if deadline is None else deadline
            if not math.isfinite(deadline) or deadline > p['deadline']:
                raise ValueError('child extends deadline')
            if model is not None and model not in json.loads(db.execute('SELECT config FROM arms WHERE id=?', (p['arm'],)).fetchone()[0])['models']:
                raise AdmissionDenied('model-not-allowed')
            if p['model'] and model not in (None, p['model']): raise AdmissionDenied('model-pool')
            model = model or p['model']
            old = db.execute('SELECT id FROM accounts WHERE id=?', (account,)).fetchone()
            if not old:
                allocated = db.execute('SELECT COALESCE(SUM(cap),0) FROM accounts WHERE parent=?', (parent,)).fetchone()[0]
                direct = db.execute('SELECT COUNT(*) FROM attempts WHERE account=?', (parent,)).fetchone()[0]
                if direct or allocated + cap > self._cap(db, p): raise AdmissionDenied('allocation-limit')
            self._account(db, account, parent, p['arm'], cap, purpose, model, deadline)
            self.event(db, 'account', dict(id=account, parent=parent, cap=cap))

    def register_route(self, model, *, capacity, arm_limits, reserved_solos=()):
        integer(capacity, positive=True)
        if model not in OSS_ROUTES: raise AdmissionDenied('model-not-allowed')
        if any(type(v) is not int or v < 1 or v > capacity for v in arm_limits.values()):
            raise ValueError('invalid route capacity')
        if len(reserved_solos) > capacity or not set(reserved_solos) <= set(arm_limits):
            raise ValueError('invalid solo reservations')
        config = canonical(dict(capacity=capacity, arms=arm_limits, solos=sorted(reserved_solos)))
        with self.transaction() as db:
            old = db.execute('SELECT config FROM routes WHERE id=?', (model,)).fetchone()
            if old and old[0] != config: raise AdmissionDenied('immutable-route')
            db.execute('INSERT OR IGNORE INTO routes VALUES (?,?)', (model, config))

    def claim_client(self, ctx):
        """Cohort-wide namespace capacity; all swarm roles count, no paid preemption.

        Qualified solo namespaces retain their fixed one-client reservation. The
        actual upstream slots remain separately enforced by reserve().
        """
        key=ctx.arm+':'+ctx.worker
        with self.transaction() as db:
            row=db.execute('SELECT config FROM routes WHERE id=?',(ctx.model,)).fetchone()
            if not row:raise AdmissionDenied('route-unqualified')
            route=json.loads(row[0])
            if ctx.arm not in route['arms']:raise AdmissionDenied('arm-route-unqualified')
            saved=db.execute("SELECT value FROM settings WHERE key='client-slots'").fetchone()
            state=json.loads(saved[0]) if saved else dict(active={},waiting={})
            value=dict(arm=ctx.arm,worker=ctx.worker,model=ctx.model,role=ctx.role)
            if key in state['active']:
                if state['active'][key]!=value:raise AdmissionDenied('client-identity-conflict')
                return True
            state['waiting'].setdefault(key,dict(**value,at=self.clock()))
            waiters=sorted(((k,v) for k,v in state['waiting'].items() if v['model']==ctx.model),
                key=lambda pair:(pair[1]['role']!='orchestrator',pair[1]['at'],pair[0]))
            active=[v for v in state['active'].values() if v['model']==ctx.model]
            arm_limit=route['arms'][ctx.arm]
            arm_active=sum(v['arm']==ctx.arm for v in active)
            # Keep one slot of the arm's orchestrator model for its orchestrator
            # (live swarm check 9: two K3 workers starved mixed-swarm's K3 orchestrator).
            orchestrator_model={'mixed-swarm':'moonshotai/kimi-k3','dsf-swarm':'deepseek/deepseek-v4.1-flash'}.get(ctx.arm)
            if ctx.role!='orchestrator' and ctx.model==orchestrator_model and arm_limit>=2:
                arm_limit-=1
                arm_active=sum(v['arm']==ctx.arm and v['role']!='orchestrator' for v in active)
            allowed=True  # no client-slot limits (operator ruling 2026-10-05 ~12:05 PDT)
            if allowed:state['active'][key]=value;del state['waiting'][key]
            db.execute("INSERT OR REPLACE INTO settings VALUES ('client-slots',?)",(canonical(state),))
            return allowed

    def release_client(self, ctx, *, fence_proof):
        if not fence_proof:raise ValueError('physical namespace fence required')
        with self.transaction() as db:
            row=db.execute("SELECT value FROM settings WHERE key='client-slots'").fetchone()
            if not row:return
            state=json.loads(row[0]);key=ctx.arm+':'+ctx.worker
            state['active'].pop(key,None);state['waiting'].pop(key,None)
            db.execute("INSERT OR REPLACE INTO settings VALUES ('client-slots',?)",(canonical(state),))

    def _chain(self, db, account):
        chain = []
        while account is not None:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account,)).fetchone()
            if not row: raise AdmissionDenied('unknown-account')
            chain.append(row)
            account = row['parent']
        return chain

    def _totals(self, db, account=None):
        if account is None:
            rows = db.execute('SELECT * FROM attempts').fetchall()
        else:
            rows = db.execute("""WITH RECURSIVE subtree(id) AS (
                SELECT id FROM accounts WHERE id=? UNION ALL
                SELECT a.id FROM accounts a JOIN subtree s ON a.parent=s.id)
                SELECT r.* FROM attempts r JOIN subtree s ON r.account=s.id""", (account,)).fetchall()
        out = {k: sum(r[k] for r in rows) for k in ('bound', 'settled', 'liability', 'reservation', 'released', 'overrun')}
        # 'settled' includes provisional settlements; 'provisional' is that part of it.
        marked = self._provisional_ids(db)
        out['provisional'] = sum(r['settled'] for r in rows if r['id'] in marked)
        out['provisional_attempts'] = sum(r['id'] in marked for r in rows)
        out['estimated'] = sum(r['estimate'] or 0 for r in rows if r['state'] != 'settled')
        out['committed'] = out['settled'] + out['liability'] + out['reservation']
        out['residual'] = out['bound'] + out['overrun'] - out['committed'] - out['released']
        return out

    @staticmethod
    def _provisional_ids(db):
        return {r[0][len(PROVISIONAL):] for r in db.execute("SELECT key FROM settings WHERE key LIKE 'provisional:%'")}

    def _settle_provisional(self, db, now, exclude=()):
        settled = amount = 0
        for row in db.execute("SELECT * FROM attempts WHERE state='unresolved' AND estimate IS NOT NULL AND ended IS NOT NULL AND slot=0").fetchall():
            if row['id'] in exclude:continue
            observed = db.execute('SELECT value FROM settings WHERE key=?', ('lookup-ended:'+row['id'],)).fetchone()
            ended = float(observed[0]) if observed else row['ended']
            if now < ended + PROVISIONAL_AFTER:continue
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (PROVISIONAL+row['id'],
                       canonical(dict(amount=row['estimate'], at=now, lookups=row['lookups']))))
            self._settle(db, row['id'], row['estimate'])
            self.event(db, 'provisional-settle', dict(id=row['id'], amount=row['estimate'], lookups=row['lookups'],
                       model=json.loads(row['context']).get('model')))
            settled += 1; amount += row['estimate']
        if settled:
            marked = self._provisional_ids(db)
            open_amount = sum(r['settled'] for r in db.execute('SELECT id,settled FROM attempts') if r['id'] in marked)
            self.event(db, 'provisional-settlement', dict(attempts=settled, microusd=amount, at=now,
                       open_attempts=len(marked), open_microusd=open_amount))
        return dict(attempts=settled, microusd=amount)

    def settle_provisional(self, *, exclude=()):
        """Settle finished replies whose cost record is still unavailable at their estimate."""
        with self.transaction() as db:
            return self._settle_provisional(db, self.lookup_clock(), exclude)

    def provisional_ids(self):
        with self.transaction() as db:
            return self._provisional_ids(db)

    def provisional_summary(self):
        return provisional_summary(self.checkpoint()['tables'])

    def _audit(self, db):
        rows = db.execute('SELECT * FROM attempts').fetchall()
        admitted = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='reserve'")]
        # Provider health probes are booked settled at their actual charge (record_probe) with a
        # 'probe' event, never a reservation: their charge is their admitted bound.
        admitted += [dict(id=p['id'], bound=max(1, int(p['cost']))) for p in
                     (json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='probe'")) if 'id' in p and 'cost' in p]
        by_id = {r['id']:r['bound'] for r in admitted}
        if len(by_id) != len(admitted) or by_id != {r['id']:r['bound'] for r in rows}:
            return sum(r['bound'] for r in admitted)-sum(r['bound'] for r in rows) or 1
        for row in rows:
            residual = row['bound']+row['overrun']-row['settled']-row['liability']-row['reservation']-row['released']
            if residual:return residual
            active = {'reserved':'reservation','unresolved':'liability','settled':'settled'}.get(row['state'])
            if active is None or any(row[k] for k in ('settled','liability','reservation') if k!=active):return 1
            # An unresolved reply with in-stream cost holds part of its bound and has released the rest.
            if active!='settled' and (row[active]+row['released']!=row['bound']+row['overrun'] or (row['released'] and row['state']!='unresolved')):return 1
        return 0

    def assert_conservation(self, *, expected_committed=None):
        with self.transaction() as db:
            residual = self._audit(db)
            if expected_committed is not None:
                integer(expected_committed)
                residual = residual or expected_committed - self._totals(db)['committed']
            if residual:
                self.event(db, 'accounting-anomaly', dict(reason='conservation-residual', residual=residual))
        if residual: raise AdmissionDenied('conservation-residual')
        return self.totals()

    def totals(self, account=None, *, expected_committed=None):
        with self.transaction() as db:
            out = self._totals(db, account)
            if expected_committed is not None:
                integer(expected_committed)
                out['external_residual'] = expected_committed - out['committed']
            return out

    def admission_forecast(self, arm):
        from .budget import forecast
        with self.transaction() as db:
            setting=db.execute("SELECT value FROM settings WHERE key='operator-fresh-p01-hard-cap'").fetchone()
            hard=bool(setting and setting[0]==arm)
            return {a['id']:forecast(self._cap(db,a),self._totals(db,a['id']),hard=hard)
                    for a in db.execute('SELECT * FROM accounts WHERE arm=?',(arm,))}

    def totals_many(self, accounts):
        """One atomic cohort snapshot, avoiding per-pool write-lock churn."""
        with self.transaction() as db:
            return {account:self._totals(db,account) for account in accounts}

    def _validate(self, db, ctx, now):
        if ctx.model not in OSS_ROUTES or OSS_ROUTES[ctx.model] != ctx.provider:
            raise AdmissionDenied('model-route-not-allowed')
        arm = db.execute('SELECT * FROM arms WHERE id=?', (ctx.arm,)).fetchone()
        if arm is None: raise AdmissionDenied('unknown-arm')
        config = json.loads(arm['config'])
        if ctx.model not in config['models']: raise AdmissionDenied('model-not-allowed')
        if ctx.role not in ROLES: raise AdmissionDenied('role-not-allowed')
        if ctx.request_class not in {'primary','secondary','internal-verification','peer-review','report','specialist','auxiliary','orchestration'}:
            raise AdmissionDenied('request-class-not-allowed')
        cohort = db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()
        if cohort:
            registration = json.loads(cohort[0])
            if not db.execute("SELECT 1 FROM settings WHERE key='profile-configured'").fetchone():
                raise AdmissionDenied('profile-configuration-incomplete')
            if ctx.cohort != registration['id'] or ctx.problem != registration['problem']:
                raise AdmissionDenied('cohort-identity')
        if arm['mode'] == 'terminal': raise AdmissionDenied('terminal')
        if db.execute("SELECT 1 FROM settings WHERE key='operator-paused'").fetchone():
            raise AdmissionDenied('operator-paused')
        if arm['pause']: raise AdmissionDenied(arm['pause'])
        if (db.execute("SELECT 1 FROM settings WHERE key='provider-refusal'").fetchone() or
                getattr(self,'provider_account',None) and self.provider_account.snapshot()['paused']):
            raise AdmissionDenied('provider-refusal')
        if now < config['start'] or now >= config['deadline']: raise AdmissionDenied('deadline')
        if arm['mode'] == 'report' and ctx.role not in {'report', 'verify', 'review', 'recovery', 'orchestrator'}:
            raise AdmissionDenied('report-lock')
        if db.execute("SELECT 1 FROM settings WHERE key='blocked'").fetchone():
            raise AdmissionDenied('accounting-blocked')
        if self._audit(db): raise AdmissionDenied('conservation-residual')
        chain = self._chain(db, ctx.account)
        if chain[0]['arm'] != ctx.arm: raise AdmissionDenied('cross-arm-account')
        if db.execute('SELECT 1 FROM accounts WHERE parent=?', (ctx.account,)).fetchone():
            raise AdmissionDenied('nonleaf-account')
        for a in chain:
            if now >= a['deadline']: raise AdmissionDenied('deadline')
            if a['model'] and a['model'] != ctx.model: raise AdmissionDenied('model-pool')
            if a['purpose'] in ROLES and a['purpose'] != ctx.role:
                # Solo internal verification is paid from the research pool while open.
                permitted = (a['purpose']=='research' and ctx.role=='verify' and arm['mode']=='research' or
                             a['purpose']=='report' and ctx.role in ('verify','review') and arm['mode']=='report' or
                             a['purpose']=='review' and ctx.role=='verify' and arm['mode']=='report')
                if not permitted:raise AdmissionDenied('role-pool')
        purpose = chain[0]['purpose']
        if purpose.startswith('closing-'):
            permitted = {'closing-writer': {'report'}, 'closing-verifier-a': {'verify','review'},
                         'closing-verifier-b': {'verify','review'}, 'closing-control': {'orchestrator'}}
            extra_check=(purpose=='closing-control' and ctx.role=='verify' and ctx.phase.startswith('extra-'))
            if arm['mode'] != 'report' or (ctx.role not in permitted.get(purpose, set()) and not extra_check):
                raise AdmissionDenied('closing-role-reserve')
            if purpose == 'closing-writer':
                from .writer_style import writer_pair
                from .open_swarm import MODEL_IDS
                allowed = ({MODEL_IDS[k] for k in writer_pair('mixed-swarm')} if ctx.arm == 'mixed-swarm'
                           else {'deepseek/deepseek-v4.1-flash'})
                if ctx.arm != 'open-swarm' and ctx.model not in allowed: raise AdmissionDenied('writer-model')  # measured writer pair; Flash for other arms; open-swarm: every member writes
        if purpose == 'dynamic-research' and (arm['mode'] != 'research' or ctx.role == 'report'):
            raise AdmissionDenied('research-closed')
        if ctx.role == 'orchestrator':
            # Operator 2026-10-09: mixed-swarm's coordinator may run Flash (roster.coordinator) as well as K3.
            required = ('moonshotai/kimi-k3', 'deepseek/deepseek-v4.1-flash') if ctx.arm == 'mixed-swarm' else ('deepseek/deepseek-v4.1-flash',)
            if ctx.model not in required or ctx.request_class != 'orchestration' or ctx.tool_bound or ctx.search_provenance:
                raise AdmissionDenied('orchestrator-identity')
            if ctx.max_input_tokens > 64000:
                raise AdmissionDenied('orchestrator-request-shape')
        if ctx.model == 'z-ai/glm-5.3' and any(a['purpose'] == 'recovery' for a in chain):
            raise AdmissionDenied('specialist-recovery-cap')
        if ctx.role == 'review' and ctx.max_input_tokens > 64000:
            raise AdmissionDenied('review-request-shape')
        if ctx.model == 'z-ai/glm-5.3' and ctx.max_input_tokens > 64000:
            raise AdmissionDenied('specialist-request-shape')
        integer(ctx.max_input_tokens, positive=True); integer(ctx.max_output_tokens, positive=True)
        from .output_limits import provider_max
        if ctx.max_input_tokens>196608 or ctx.max_output_tokens>provider_max(ctx.model):raise AdmissionDenied('request-shape')
        integer(ctx.tool_bound)
        if not ctx.credential: raise AdmissionDenied('credential-identity-required')
        return chain

    def reserve(self, ctx, bound, *, attempt_id=None, deadline=None):
        """Atomic upper-bound and aggregate slot admission; no network operation.

        A caller supplies a stable attempt ID for idempotence, but an existing ID
        never authorizes another send. Retries use NEW attempt IDs and retain old
        liability. Denied slot requests persist their FIFO position.
        """
        from dataclasses import asdict
        integer(bound, positive=True)
        rid = attempt_id or uuid.uuid4().hex
        if ctx.role in ('research', 'review', 'specialist'):
            # The research stop never waits on the provider's cost records. Its own
            # transaction, so a budget denial below cannot roll it back.
            self.settle_provisional()
        now = self.clock()
        denial = None
        with self.transaction() as db:
            context = canonical(asdict(ctx))
            old = db.execute('SELECT * FROM attempts WHERE id=?', (rid,)).fetchone()
            if old:
                if old['context'] != context or old['bound'] != bound:
                    raise AdmissionDenied('attempt-id-conflict')
                return dict(old)
            chain = self._validate(db, ctx, now)
            from .opening_approaches import budget_check
            budget_check(db, ctx, bound)
            if ctx.logical_request:
                previous = [r for r in db.execute('SELECT * FROM attempts WHERE arm=?',(ctx.arm,))
                            if json.loads(r['context']).get('logical_request') == ctx.logical_request]
                # Provider 429s/faults retry until the incident deadline (operator); 20 attempts
                # bounds a broken loop. A completed reply still ends the logical request.
                if len(previous)>=20 or any(r['outcome'] in ('normal','output-truncated','reasoning-exhausted') for r in previous):
                    raise AdmissionDenied('logical-request-exhausted')
                if previous and now >= min(r['created'] for r in previous)+__import__('v2.gateway',fromlist=['x']).request_deadline_seconds(ctx.model):
                    raise AdmissionDenied('incident-deadline')
            if ctx.worker != 'unregistered' and any(json.loads(r['context']).get('worker')==ctx.worker
                for r in db.execute('SELECT context FROM attempts WHERE slot=1 AND arm=?',(ctx.arm,))):
                raise AdmissionDenied('worker-slot-busy')
            deadline = min(a['deadline'] for a in chain) if deadline is None else min(deadline, *(a['deadline'] for a in chain))
            if not math.isfinite(deadline) or not now < deadline <= min(a['deadline'] for a in chain):
                raise AdmissionDenied('request-deadline')
            for a in chain:
                total = self._totals(db, a['id'])
                if total['residual']: raise AdmissionDenied('conservation-residual')
                hard = db.execute("SELECT value FROM settings WHERE key='operator-fresh-p01-hard-cap'").fetchone()
                if hard and ctx.arm == hard[0] and not __import__('v2.budget',fromlist=['x']).financial_admission(self._cap(db,a),total,bound=bound,hard=True):
                    raise AdmissionDenied('budget')
            # Operator ruling 2026-10-05 ~10:00 PDT: no pre-request reservation limits,
            # liability limits or closing reserves. Money in flight never refuses a
            # request. The only money rule: once SETTLED arm spend reaches the arm
            # budget, no new research; closing (writer, checks, orchestrator) always runs.
            arm_total = self._totals(db, chain[-1]['id'])  # chain runs leaf -> arm root
            arm_mode = db.execute('SELECT mode FROM arms WHERE id=?', (ctx.arm,)).fetchone()[0]
            if ctx.role in ('research', 'review', 'specialist') and arm_mode == 'research' and not __import__('v2.budget',fromlist=['x']).financial_admission(self._cap(db,chain[-1]),arm_total,research=True):
                raise AdmissionDenied('budget')
            route_row = db.execute('SELECT config FROM routes WHERE id=?', (ctx.model,)).fetchone()
            if not route_row: raise AdmissionDenied('route-unqualified')
            route = json.loads(route_row[0])
            ramp_row=db.execute("SELECT value FROM settings WHERE key='pilot-route-ramp'").fetchone()
            if ramp_row:
                ramp=json.loads(ramp_row[0])
                limit=ramp['routes'][ctx.model]['admission_capacity']
                if type(limit) is not int or limit < 1:
                    raise AdmissionDenied('invalid-pilot-route-ramp')
                # A ramp rung above this profile's route capacity is capped, never
                # a refusal (diag-1113: swarm/solo checks have smaller routes).
                route['capacity']=min(limit, route['capacity'])
            if ctx.arm not in route['arms']: raise AdmissionDenied('arm-route-unqualified')
            active = [r for r in db.execute('SELECT arm,context FROM attempts WHERE slot=1')
                      if json.loads(r['context'])['model'] == ctx.model]
            solo = ctx.arm in route['solos']
            swarm_active = sum(r['arm'] not in route['solos'] for r in active)
            db.execute('INSERT OR IGNORE INTO waiters(id,route,arm,created,priority) VALUES (?,?,?,?,?)', (rid, ctx.model, ctx.arm, now, int(ctx.role=='orchestrator')))
            waiters = db.execute('SELECT id,arm FROM waiters WHERE route=? ORDER BY priority DESC,rowid', (ctx.model,)).fetchall()
            first = next((r['id'] for r in waiters if (r['arm'] in route['solos']) == solo), rid)
            # Operator ruling 2026-10-05 ~12:05 PDT ("no reason to limit"): no
            # per-arm or per-route concurrency slots; only provider 429/overload
            # responses throttle, through gateway backoff and retry.
            if False:
                denial = 'route-busy'
            else:
                receipt = hashlib.sha256(canonical(dict(id=rid, context=json.loads(context), bound=bound,
                                                        deadline=deadline, created=now)).encode()).hexdigest()
                db.execute('''INSERT INTO attempts(id,arm,account,context,bound,state,reservation,created,deadline,receipt)
                              VALUES (?,?,?,?,?,'reserved',?,?,?,?)''',
                           (rid, ctx.arm, ctx.account, context, bound, bound, now, deadline, receipt))
                db.execute('DELETE FROM waiters WHERE id=?', (rid,))
                self.event(db, 'reserve', dict(id=rid, bound=bound, receipt=receipt))
                result = dict(db.execute('SELECT * FROM attempts WHERE id=?', (rid,)).fetchone())
        if denial: raise AdmissionDenied(denial)
        return result

    def cancel_waiter(self, attempt_id):
        with self.transaction() as db:
            db.execute('DELETE FROM waiters WHERE id=?', (attempt_id,))

    def cancel_unsent(self, attempt_id):
        """Release only a local authorization that never crossed mark_sent.

        Serialized with mark_sent: the gateway opens transport only after that
        durable marker. Fenced/ambiguous restore sets sent=1 and cannot use this.
        """
        with self.transaction() as db:
            row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None or row['state']!='reserved' or row['sent'] or row['generation']:
                return False
            self._settle(db,attempt_id,0)
            db.execute('UPDATE attempts SET ended=?,outcome=? WHERE id=?',
                       (self.clock(),'admission-refused-before-send',attempt_id))
            self.event(db,'unsent-admission-cancelled',dict(id=attempt_id))
            return True

    def mark_sent(self, attempt_id, *, durable_receipt):
        """Called only after external publisher ACK. Persist before opening socket."""
        with self.transaction() as db:
            row = self._attempt(db, attempt_id)
            if row['sent'] or row['state'] != 'reserved': raise AdmissionDenied('attempt-already-sent')
            if durable_receipt != row['receipt']: raise AdmissionDenied('durability-unacknowledged')
            ctx = AdmissionContext(**json.loads(row['context']))
            self._validate(db, ctx, self.clock())
            if self.clock() >= row['deadline']: raise AdmissionDenied('request-deadline')
            db.execute('UPDATE attempts SET sent=1 WHERE id=?', (attempt_id,))
            self.event(db, 'send', dict(id=attempt_id,at=self.clock()))

    @staticmethod
    def _attempt(db, attempt_id):
        row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
        if row is None: raise ValueError('unknown attempt')
        return row

    def finish(self, attempt_id, *, generation=None, estimate=None, outcome='unknown', authoritative_cost=None, upstream_done=None, usage_reported=False, known_bound=None, provider_end=None):
        """Retain an uncertain bill AND its slot unless upstream completion is known.

        Estimates annotate liability. An observation above the admitted bound is
        an explicit invariant failure, retained conservatively, never clamped.
        """
        if estimate is not None: integer(estimate)
        if upstream_done is None:
            # diag-1328: a provider error ends the request on the wire, so its slot is
            # released at once; only the money stays unresolved pending the lookup.
            # A mismatched-model response is also closed by the gateway before
            # finish. Keep its uncertain bill, but let the same worker retry.
            upstream_done=outcome in {'normal','output-truncated','reasoning-exhausted','http-error','context','tool-complete','in-stream-error','transport-loss','timeout','server-tool-boundary','model-mismatch','off-route-rejected'}
        error=None
        with self.transaction() as db:
            row = self._attempt(db, attempt_id)
            if row['state'] == 'settled':
                if authoritative_cost is not None:error=self._settle(db,attempt_id,authoritative_cost)
            elif generation and row['generation'] and generation != row['generation']:
                error='generation-conflict'
                self.event(db, 'accounting-anomaly', dict(reason=error,attempt=attempt_id))
            else:
                estimate=row['estimate'] if estimate is None else estimate
                liability=max(row['bound'],row['liability'],estimate or 0)
                # Live swarm check 6: replies are uncapped, so each bound is a
                # provider-maximum request ($1.15 DSF, $14.35 K3) while metadata
                # lookups run one at a time from 60 s after the reply. Holding the
                # whole bound per finished reply exhausted the research pool. When
                # the provider reported the reply's cost in-stream and upstream is
                # done, hold twice that cost (minimum $0.01) until metadata settles.
                if usage_reported and upstream_done and estimate is not None and row['liability']==0:
                    # A reported cost above the admitted bound is held whole (overrun anomaly).
                    liability=max(min(row['bound'],max(2*estimate,estimate+10000)),estimate)
                # rdcheck2: research replies with server search never counted as usage-reported,
                # so each finished K3 reply held its whole $14.49 provider-maximum bound although
                # the provider had reported its tokens. known_bound is a safe upper bound built
                # from the reported token counts and prices plus every unreported fee bound
                # (search, tools); twice it is held until metadata settles.
                elif known_bound is not None and upstream_done and row['liability']==0:
                    integer(known_bound)
                    # Never clamp a known charge: above the admitted bound it is held whole
                    # and reported as an overrun anomaly below.
                    liability=max(min(row['bound'],max(2*known_bound,known_bound+10000)),known_bound,estimate or 0)
                overrun=max(0,liability-row['bound'])
                outcome=row['outcome'] if outcome=='unknown' and row['outcome'] else outcome
                db.execute("""UPDATE attempts SET state='unresolved',liability=?,overrun=?,reservation=0,released=?,
                           slot=?,ended=COALESCE(ended,?),generation=COALESCE(generation,?),estimate=?,outcome=? WHERE id=?""",
                           (liability,overrun,max(0,row['bound']-liability),0 if upstream_done else row['slot'],self.clock(),generation,estimate,outcome,attempt_id))
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',
                           ('lookup-ended:'+attempt_id,str(self.lookup_clock())))
                if overrun:
                    self.event(db, 'accounting-anomaly', dict(reason='estimate-over-bound',attempt=attempt_id))
                if authoritative_cost is not None:error=self._settle(db,attempt_id,authoritative_cost)
                self.event(db, 'finish', dict(id=attempt_id, outcome=outcome))
            if provider_end is not None:
                if provider_end.get('id')!=attempt_id:raise ValueError('provider end identity mismatch')
                self.event(db,'provider-end',provider_end)
            saved=dict(self._attempt(db,attempt_id))
        if error:raise AdmissionDenied(error)
        return saved

    def _settle(self, db, attempt_id, cost):
        integer(cost)
        row = self._attempt(db, attempt_id)
        if row['state'] == 'settled' and db.execute('SELECT 1 FROM settings WHERE key=?', (PROVISIONAL+attempt_id,)).fetchone():
            # The provider's real record replaces a provisional settlement, up or down.
            overrun = max(0, cost - row['bound'])
            db.execute('UPDATE attempts SET settled=?,released=?,overrun=? WHERE id=?',
                       (cost, max(0, row['bound'] - cost), overrun, attempt_id))
            db.execute('DELETE FROM settings WHERE key=?', (PROVISIONAL+attempt_id,))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('provisional-corrected:'+attempt_id,
                       canonical(dict(provisional=row['settled'], actual=cost, delta=cost-row['settled']))))
            if overrun and not row['overrun']:
                self.event(db, 'accounting-anomaly', dict(reason='cost-over-bound', attempt=attempt_id))
            self.event(db, 'provisional-corrected', dict(id=attempt_id, provisional=row['settled'], actual=cost, delta=cost-row['settled']))
            return
        if row['state'] == 'settled':
            if cost != row['settled']:
                self.event(db, 'accounting-anomaly', dict(reason='conflicting-settlement',attempt=attempt_id))
                return 'conflicting-settlement'
            return
        overrun = max(0, cost - row['bound'])
        db.execute('''UPDATE attempts SET state='settled',settled=?,liability=0,reservation=0,slot=0,
                      released=?,overrun=? WHERE id=?''',
                   (cost, max(0, row['bound'] - cost), overrun, attempt_id))
        if overrun:
            self.event(db, 'accounting-anomaly', dict(reason='cost-over-bound', attempt=attempt_id))
        self.event(db, 'settle', dict(id=attempt_id, cost=cost, released=max(0,row['bound']-cost), overrun=overrun))

    def settle(self, attempt_id, cost):
        with self.transaction() as db: error=self._settle(db, attempt_id, cost)
        if error:raise AdmissionDenied(error)

    def anomaly(self, reason, **detail):
        with self.transaction() as db:
            self.event(db, 'accounting-anomaly', dict(reason=reason, **detail))

    def block(self, reason):
        """Controller stops only (shutdown, restore barrier, invariant stop). Model-side
        anomalies are logged, never blocks (operator ruling 2026-10-06)."""
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('blocked',?)", (canonical(dict(reason=reason)),))
            self.event(db, 'admission-blocked', dict(reason=reason))

    def provider_refused(self, status, detail, model, provider):
        circuit = getattr(self, 'provider_account', None)
        if circuit:circuit.refuse(status, detail, model, provider)
        from .pause import RunClock
        if isinstance(self.clock, RunClock):self.clock.pause('provider')
        from .provider_refusal import metadata
        cause = dict(status=status,model=model,provider=provider,metadata=metadata(detail))
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('provider-refusal',?)", (canonical(cause),))
            self.event(db, 'provider-refusal', dict(status=status, model=model, provider=provider))

    def reserve_health_probe(self, arm, model, provider, bound, *, credential, dry_run=False):
        """Private controller probe admission, never reachable from worker requests.

        Bypass only the provider pause: operator/invariant/terminal barriers hold.
        Keep a bounded reservation in the existing control account and full ledger.
        """
        from dataclasses import asdict
        integer(bound, positive=True)
        if bound > 10000:raise AdmissionDenied('probe-price-bound')
        rid = 'account-probe:' + uuid.uuid4().hex
        with self.transaction() as db:
            row = db.execute('SELECT * FROM arms WHERE id=?', (arm,)).fetchone()
            if not row or row['mode']=='terminal' or row['pause']:raise AdmissionDenied('probe-arm-closed')
            if db.execute("SELECT 1 FROM settings WHERE key IN ('operator-paused','blocked')").fetchone():
                raise AdmissionDenied('probe-controller-closed')
            if model not in json.loads(row['config'])['models'] or OSS_ROUTES.get(model)!=provider:
                raise AdmissionDenied('probe-route-unqualified')
            route = db.execute('SELECT config FROM routes WHERE id=?',(model,)).fetchone()
            if not route or arm not in json.loads(route[0])['arms']:raise AdmissionDenied('probe-route-unqualified')
            now = self.clock()
            account = db.execute("SELECT id FROM accounts WHERE id=?",(arm+'/control',)).fetchone()
            if account is None:
                account = db.execute("""SELECT a.id FROM accounts a WHERE a.arm=?
                    AND a.purpose IN ('report','verify','recovery','closing-control','orchestrator')
                    AND (a.model IS NULL OR a.model=?) AND a.deadline>?
                    AND NOT EXISTS (SELECT 1 FROM accounts child WHERE child.parent=a.id)
                    ORDER BY a.id LIMIT 1""",(arm,model,now)).fetchone()
            if account is None:raise AdmissionDenied('probe-account-unavailable')
            ctx = AdmissionContext(arm,account[0],model,provider,credential,
                                   role='recovery',worker='harness-account-probe',request_class='auxiliary',
                                   max_input_tokens=128,max_output_tokens=1,phase='provider-health')
            chain = self._chain(db,ctx.account)
            deadline = min(a['deadline'] for a in chain)
            if now >= deadline:raise AdmissionDenied('probe-deadline')
            if self._audit(db):raise AdmissionDenied('conservation-residual')
            hard = db.execute("SELECT value FROM settings WHERE key='operator-fresh-p01-hard-cap'").fetchone()
            if hard and arm == hard[0] and any(not __import__('v2.budget',fromlist=['x']).financial_admission(
                    self._cap(db,a),self._totals(db,a['id']),bound=bound,hard=True) for a in chain):
                raise AdmissionDenied('probe-budget')
            if dry_run:return True
            context = canonical(asdict(ctx))
            receipt = hashlib.sha256(canonical(dict(id=rid,context=context,bound=bound)).encode()).hexdigest()
            db.execute("""INSERT INTO attempts(id,arm,account,context,bound,state,reservation,created,deadline,receipt)
                          VALUES (?,?,?,?,?,'reserved',?,?,?,?)""",
                       (rid,arm,ctx.account,context,bound,bound,now,deadline,receipt))
            self.event(db,'reserve',dict(id=rid,bound=bound,receipt=receipt,health_probe=True))
            return dict(db.execute('SELECT * FROM attempts WHERE id=?',(rid,)).fetchone())

    def send_health_probe(self, rid, receipt):
        with self.transaction() as db:
            row = self._attempt(db,rid)
            if not rid.startswith('account-probe:') or row['sent'] or row['state']!='reserved' or row['receipt']!=receipt:
                raise AdmissionDenied('invalid-probe-reservation')
            arm = db.execute('SELECT mode,pause FROM arms WHERE id=?',(row['arm'],)).fetchone()
            if (arm['mode']=='terminal' or arm['pause'] or self.clock()>=row['deadline'] or
                db.execute("SELECT 1 FROM settings WHERE key IN ('operator-paused','blocked')").fetchone()):
                raise AdmissionDenied('probe-controller-closed')
            db.execute('UPDATE attempts SET sent=1 WHERE id=?',(rid,))
            self.event(db,'send-health-probe',dict(id=rid))

    def record_probe(self, arm, model, provider, cost):
        """A provider health probe's actual charge, booked to the arm's control account."""
        import uuid
        cost = max(1, int(cost))
        rid = 'probe:' + arm + ':' + uuid.uuid4().hex
        context = canonical(dict(arm=arm, account=arm + '/control', model=model, provider=provider, role='probe', worker='harness-probe'))
        with self.transaction() as db:
            db.execute("""INSERT INTO attempts(id,arm,account,context,bound,state,reservation,created,ended,deadline,receipt,slot)
                          VALUES (?,?,?,?,?,'unresolved',0,?,?,?,?,0)""",
                       (rid, arm, arm + '/control', context, cost, self.clock(), self.clock(), self.clock(), rid))
            self._settle(db, rid, cost)
            self.event(db, 'probe', dict(id=rid, model=model, provider=provider, cost=cost))
        return rid

    def unreserved_tool_charge(self, ctx, event_id, cost):
        """Record an observed protocol violation without hiding the actual charge.

        This is NOT admission: no upper bound was reserved before execution. The
        zero original bound plus explicit overrun deliberately fails P1 acceptance.
        """
        from dataclasses import asdict
        integer(cost, positive=True)
        rid='unreserved-tool:'+ctx.arm+':'+event_id
        with self.transaction() as db:
            row=db.execute('SELECT * FROM attempts WHERE id=?',(rid,)).fetchone()
            if not row:
                context=canonical(asdict(ctx))
                receipt=hashlib.sha256(rid.encode()).hexdigest()
                db.execute("""INSERT INTO attempts(id,arm,account,context,bound,state,reservation,created,ended,deadline,receipt,slot)
                              VALUES (?,?,?,?,0,'unresolved',0,?,?,?,?,0)""",
                           (rid,ctx.arm,ctx.account,context,self.clock(),self.clock(),self.clock(),receipt))
                self.event(db,'reserve',dict(id=rid,bound=0,receipt=receipt,violation='unreserved-tool'))
                self._settle(db,rid,cost)
            self.event(db, 'accounting-anomaly', dict(reason='unreserved-tool-charge',attempt=rid))
        return rid

    def fence_attempt(self, attempt_id, *, proof):
        """Controller confirms old sender/upstream cancellation is bounded/fenced.

        Liability stays retained. Mere elapsed time is not a fence proof.
        """
        if not proof: raise ValueError('fence proof required')
        with self.transaction() as db:
            row = self._attempt(db, attempt_id)
            if row['state'] != 'settled':
                db.execute("UPDATE attempts SET state='unresolved',reservation=0,liability=bound+overrun,slot=0,ended=COALESCE(ended,?) WHERE id=?", (self.clock(), attempt_id))
            else: db.execute('UPDATE attempts SET slot=0 WHERE id=?', (attempt_id,))
            self.event(db, 'fence', dict(id=attempt_id, proof=proof))

    def control(self, control_id, arm, *, mode=None, pause=None):
        if mode not in (None, 'report', 'terminal'): raise ValueError('one-way mode only')
        payload = canonical(dict(arm=arm, mode=mode, pause=pause))
        with self.transaction() as db:
            old = db.execute('SELECT payload FROM controls WHERE id=?', (control_id,)).fetchone()
            if old:
                if old[0] != payload: raise AdmissionDenied('control-id-conflict')
                return
            row = db.execute('SELECT * FROM arms WHERE id=?', (arm,)).fetchone()
            if row is None: raise ValueError('unknown arm')
            if row['mode'] == 'terminal' and mode != 'terminal': raise AdmissionDenied('terminal')
            db.execute('UPDATE arms SET mode=?,pause=? WHERE id=?', (mode or row['mode'], pause, arm))
            db.execute('INSERT INTO controls VALUES (?,?)', (control_id, payload))
            self.event(db, 'control', json.loads(payload))

    def release_ungenerated(self, *, min_age=900, exclude=()):
        """Live 21:57/00:40: attempts that failed before the provider returned a generation id
        can never be looked up and held their bound as liability forever. After min_age
        seconds they are settled at 0 (logged) only without an estimate. Estimated replies
        settle provisionally instead, even if the provider omitted the generation id."""
        now = self.lookup_clock(); released = 0
        with self.transaction() as db:
            rows = db.execute("SELECT id,ended FROM attempts WHERE state='unresolved' AND generation IS NULL AND estimate IS NULL AND ended IS NOT NULL AND slot=0").fetchall()
            for row in rows:
                if row['id'] in exclude:continue
                observed=db.execute('SELECT value FROM settings WHERE key=?',('lookup-ended:'+row['id'],)).fetchone()
                ended=float(observed[0]) if observed else row['ended']
                if now-ended<=min_age:continue
                self._settle(db, row['id'], 0)
                self.event(db, 'released-ungenerated', dict(id=row['id']))
                released += 1
        return released

    def release_stale_locks(self, *, max_age=120):
        """A lookup whose worker died keeps its credential lock; free locks older than max_age."""
        now = self.lookup_clock(); freed = 0
        with self.transaction() as db:
            for lock in db.execute('SELECT credential, attempt, token FROM lookup_locks').fetchall():
                at = db.execute('SELECT value FROM settings WHERE key=?', ('lookup-lock-at:' + lock['token'],)).fetchone()
                if at is None:
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('lookup-lock-at:' + lock['token'], str(now)))
                    continue
                if now - float(at[0]) >= max_age:
                    db.execute('DELETE FROM lookup_locks WHERE token=?', (lock['token'],))
                    db.execute('UPDATE attempts SET lookup_active=0 WHERE id=? AND lookup_token=?', (lock['attempt'], lock['token']))
                    db.execute('DELETE FROM settings WHERE key=?', ('lookup-lock-at:' + lock['token'],))
                    self.event(db, 'lookup-lock-freed', dict(attempt=lock['attempt']))
                    freed += 1
        return freed

    def claim_lookup(self, *, credential=None, exclude=()):
        """Reserve one of seven lifetime lookups before dispatch, one/credential.

        The controller executes it in a killed-at-15s subprocess. A crashed claim
        consumes its lookup; explicit fenced recovery clears the credential lock.
        """
        now = self.lookup_clock()
        with self.transaction() as db:
            for row in db.execute("SELECT * FROM attempts WHERE (state='unresolved' OR id IN (SELECT substr(key,13) FROM settings WHERE key LIKE 'provisional:%')) AND generation IS NOT NULL AND ended IS NOT NULL AND lookups<7 AND lookup_active=0 ORDER BY ended,id"):
                if row['id'] in exclude:continue
                index = row['lookups']
                observed=db.execute('SELECT value FROM settings WHERE key=?',('lookup-ended:'+row['id'],)).fetchone()
                ended=float(observed[0]) if observed else row['ended']
                if now >= ended + LOOKUP_HORIZON or now < ended + LOOKUP_DELAYS[index]: continue
                row_credential = json.loads(row['context'])['credential']
                if credential is not None and row_credential!=credential:continue
                if db.execute('SELECT 1 FROM lookup_locks WHERE credential=?', (row_credential,)).fetchone(): continue
                floor = db.execute('SELECT value FROM settings WHERE key=?', ('lookup-not-before:' + row_credential,)).fetchone()
                if floor and now < float(floor[0]): continue
                token = uuid.uuid4().hex
                db.execute('INSERT INTO lookup_locks VALUES (?,?,?)', (row_credential, row['id'], token))
                db.execute('UPDATE attempts SET lookups=lookups+1,lookup_active=1,lookup_token=? WHERE id=?', (token,row['id']))
                self.event(db, 'lookup', dict(id=row['id'], number=index+1, token=token))
                return dict(attempt=row['id'], generation=row['generation'], credential=row_credential, token=token, timeout=15,
                            estimate=row['estimate'],
                            complete_required=bool(json.loads(row['context'])['tool_bound'] or json.loads(row['context'])['search_provenance']))
        return None

    def complete_lookup(self, claim, *, cost=None, provider=None, retry_after=0):
        # Retry-After is an additional not-before floor, never a renewed horizon.
        if cost is not None: integer(cost)
        with self.transaction() as db:
            row = self._attempt(db, claim['attempt'])
            if not row['lookup_active'] or row['lookup_token'] != claim['token']:
                raise AdmissionDenied('stale-lookup')
            ctx = json.loads(row['context'])
            from .server_search import host_matches
            if provider:
                # Charge every served request, including rejected off-route responses.
                # Billing cannot widen the route authority or release its content.
                from .provider_routes import providers as _routes
                if not any(host_matches(p, provider) for p in _routes(ctx.get('model', ''), ctx['provider'])):
                    self.event(db, 'provider-mismatch', dict(attempt=row['id'], pinned=ctx['provider'], served=provider))
                # Operator 2026-10-07: record which provider served each looked-up call (streams often name none).
                self.event(db, 'served-host', dict(attempt=row['id'], model=ctx.get('model', ''), served=provider))
            if cost is not None: self._settle(db, row['id'], cost)
            db.execute('UPDATE attempts SET lookup_active=0 WHERE id=?', (row['id'],))
            db.execute('DELETE FROM lookup_locks WHERE token=?', (claim['token'],))
            if retry_after:
                # Persist service rate limit independently of attempt schedule.
                until = self.lookup_clock() + max(0, float(retry_after))
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('lookup-not-before:' + claim['credential'], str(until)))

    def recover_fenced(self, *, proof):
        """After P7 fences ALL old cohort senders and metadata subprocesses.

        Restored reservations are ambiguous even if the durable checkpoint says
        sent=0: the acknowledgment may have preceded a lost local send marker.
        Never reuse those send authorizations. Preserve all seven-lookup counts.
        """
        if not proof: raise ValueError('cohort fence proof required')
        with self.transaction() as db:
            uncertain={r[0] for r in db.execute("SELECT DISTINCT arm FROM attempts WHERE slot=1")}
            for arm in uncertain:
                db.execute("UPDATE arms SET pause='ambiguous-restored-request' WHERE id=? AND mode!='terminal'",(arm,))
            db.execute("""UPDATE attempts SET sent=1,slot=0,lookup_active=0,
                          ended=COALESCE(ended,?),liability=CASE WHEN state='reserved' THEN bound ELSE liability END,
                          reservation=0,state=CASE WHEN state='reserved' THEN 'unresolved' ELSE state END""", (self.clock(),))
            for row in db.execute("SELECT id FROM attempts WHERE state='unresolved' AND ended IS NOT NULL").fetchall():
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('lookup-ended:'+row['id'],str(self.lookup_clock())))
            db.execute('DELETE FROM lookup_locks')
            db.execute('DELETE FROM waiters')
            # Every old namespace is physically fenced by the whole-controller
            # proof. Rebuilt roles must reacquire current client capacity; their
            # request liabilities above are retained independently.
            db.execute("DELETE FROM settings WHERE key='client-slots'")
            self.event(db, 'cohort-fenced-recovery', dict(proof=proof))

    def checkpoint(self):
        tables = ('settings','arms','accounts','routes','attempts','waiters','events','controls','lookup_locks','grants')
        with self.transaction() as db:
            return dict(format=1, tables={name:[dict(r) for r in db.execute('SELECT * FROM '+name)] for name in tables})

    def publication_checkpoint(self, after=None):
        """An atomic base or changed rows since the preceding publication cursor.

        Deletes and replacements are keyed by the real primary key, not rowid.
        The journal is local SQLite; no external storage occurs in this lock.
        """
        with self.transaction() as db:
            cursor = db.execute('SELECT COALESCE(MAX(seq),0) FROM checkpoint_changes').fetchone()[0]
            if after is None:
                return cursor, dict(format=1, publication_cursor=cursor, tables={t:[dict(r) for r in db.execute('SELECT * FROM '+t)] for t in CHECKPOINT_KEYS})
            if not 0 <= after <= cursor: raise ValueError('invalid accounting publication cursor')
            changes = []
            for row in db.execute('SELECT DISTINCT table_name,row_key FROM checkpoint_changes WHERE seq>? ORDER BY table_name,row_key', (after,)):
                table, key = row
                value = db.execute('SELECT * FROM '+table+' WHERE '+CHECKPOINT_KEYS[table]+'=?', (key,)).fetchone()
                changes.append(dict(table=table, key=key, row=dict(value) if value is not None else None))
            return cursor, changes

    @classmethod
    def restore_checkpoint(cls, path, checkpoint, *, fence_proof, clock=time.time, lookup_clock=None):
        if Path(path).exists(): raise ValueError('restore requires a new local database')
        names = {'settings','arms','accounts','routes','attempts','waiters','events','controls','lookup_locks','grants'}
        if checkpoint.get('format') != 1 or set(checkpoint.get('tables',{})) != names:
            raise ValueError('invalid checkpoint format')
        store = cls(path,clock=clock)
        if lookup_clock is not None:store.lookup_clock=lookup_clock
        with store.transaction() as db:
            for name, rows in checkpoint['tables'].items():
                columns = [r[1] for r in db.execute('PRAGMA table_info('+name+')')]
                for row in rows:
                    if set(row) != set(columns): raise ValueError('invalid checkpoint row')
                    db.execute('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in columns)+')', [row[k] for k in columns])
        store.assert_conservation()
        store.recover_fenced(proof=fence_proof)
        return store

    def attempt(self, attempt_id):
        with self.transaction() as db: return dict(self._attempt(db, attempt_id))

    def export(self):
        with self.transaction() as db:
            return dict(totals=self._totals(db), grants=[dict(r) for r in db.execute('SELECT * FROM grants')], arms=[dict(r) for r in db.execute('SELECT * FROM arms')],
                        accounts=[dict(r) for r in db.execute('SELECT * FROM accounts')],
                        attempts=[dict(r) for r in db.execute('SELECT * FROM attempts')],
                        events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq')])


def provisional_summary(tables):
    """Provisional settlement counts and dollars from an accounting checkpoint's tables."""
    settings = {r['key']:r['value'] for r in tables.get('settings', [])}
    rows = {r['id']:r for r in tables.get('attempts', [])}
    open_ids = [k[len(PROVISIONAL):] for k in settings if k.startswith(PROVISIONAL)]
    corrected = [json.loads(v) for k, v in settings.items() if k.startswith('provisional-corrected:')]
    return dict(used=bool(open_ids or corrected),
                open_attempts=len(open_ids), open_microusd=sum(rows[i]['settled'] for i in open_ids if i in rows),
                corrected_attempts=len(corrected), corrected_delta_microusd=sum(c['delta'] for c in corrected),
                settled_microusd=sum(r['settled'] for r in rows.values()))


def _reset_accounting_locks():
    AccountingStore._locks={}
    AccountingStore._locks_guard=threading.Lock()


os.register_at_fork(after_in_child=_reset_accounting_locks)


def bounded_call(function, args=(), *, timeout):
    """Run isolated metadata/publication work under a hard wall, on Linux compute.

    No database transaction or watchdog lock may be held by the caller. A child
    stuck in storage cannot stall controller admission or renew the arm deadline.
    """
    import multiprocessing
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe(duplex=False)

    def run():
        receiver.close()
        try:
            sender.send(('ok', function(*args)))
        except BaseException as exc:
            sender.send(('error', type(exc).__name__))
        finally:
            sender.close()

    from .process import ForkProcess
    process = ForkProcess(target=run, daemon=True)
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout): raise TimeoutError('bounded operation deadline')
        status, value = receiver.recv()
        if status != 'ok': raise OSError('bounded operation failed: ' + value)
        return value
    finally:
        receiver.close()
        # Receipt delivery precedes the child's HTTP/file destructor cleanup.
        # Give a successful child time to exit normally before cancellation.
        process.join(timeout=.1)
        if process.is_alive(): process.terminate()
        process.join(timeout=.1)
        if process.is_alive(): process.kill(); process.join(timeout=.1)
        if process.is_alive():raise UnreapedOperation('bounded child could not be reaped')


class Reconciler:
    """Dispatch only currently due metadata work; never sleep until future dues."""
    def __init__(self, store, lookup, *, runner=bounded_call, credential=None):
        self.store = store
        self.lookup = lookup
        self.runner = runner
        self.credential = credential

    def drain(self, seconds=330):
        until = time.monotonic() + min(330, max(0, seconds))
        count = 0
        for fix in (self.store.release_stale_locks, self.store.release_ungenerated, self.store.settle_provisional):
            try:fix()
            except Exception:pass
        # Do not claim a lookup if the cleanup budget cannot give it its full bound.
        while time.monotonic() + 15 <= until:
            claim = self.store.claim_lookup(credential=self.credential)
            if claim is None: break
            cost = None
            provider = None
            rate_delay = 0
            try:
                record = self.runner(self.lookup, (claim['generation'],), timeout=15)
                if isinstance(record, dict):
                    provider = record.get('provider_name')
                    rate_delay = record.get('retry_after', 0)
                    # An incomplete outer tool record is not a complete bill.
                    # OpenRouter's generation record has no 'complete' flag; its total_cost
                    # includes server-tool (web search) fees. Requiring the flag left every
                    # search-capable request unsettled forever (prod-check, 19:20 PDT:
                    # 0 of 484 settled), so the settled-spend research stop never fired.
                    if record.get('total_cost') is not None and record.get('complete', True):
                        amount = micros(record['total_cost'])
                        if amount or record.get('complete') is True:cost=amount
                        # A zero placeholder without explicit completeness is not an
                        # authoritative bill. The bounded provisional pass counts the
                        # estimate while keeping later real-record lookups eligible.
            except Exception:
                # Live 00:24 (mixed): an UnreapedOperation escaped here, killed the drain thread
                # and left the lookup lock held, so nothing settled again. Any failure releases
                # the claim (retried later) and the drain continues.
                pass
            self.store.complete_lookup(claim, cost=cost, provider=provider, retry_after=rate_delay)
            count += 1
        return count
