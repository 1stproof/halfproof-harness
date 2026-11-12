"""Arm-private transactional board for v3.

Only the trusted controller instantiates Board. Workers use BoardClient, over a
sandbox-owned authenticated transport; never mount this database in a worker.
SQLite must live on node-local storage. Durable publication/admission is P7's
responsibility. No model calls, filesystem reads supplied by workers, or policy
execution occur here. All monetary values are integer microdollars.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
import unicodedata

from .contracts_v3 import CONTRACT_VERSION, contracts_sha256, load_contracts

SCHEMA_VERSION = 1
FAMILIES = {"DSF": "DeepSeek", "DSP": "DeepSeek", "Qwen": "Qwen", "K3": "Kimi", "GLM": "GLM"}
MODEL_KEYS = {"dsf": "DSF", "dsp": "DSP", "qwen24t": "Qwen", "k3": "K3", "glm": "GLM"}
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_DEPENDENCIES = 64


class BoardError(ValueError):
    """A denied operation; adapters must not disclose private exception details."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def identifier(value):
    if not isinstance(value, str):
        raise BoardError("invalid identifier")
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise BoardError("invalid identifier")
    return value


def verdict_form(record_id=None):
    """The exact verdict form every verdict refusal states."""
    return ('Post-type: verdict\nStatus: ACCEPT   (or: Status: REJECT, or: Status: objection)\nRe: %s\n'
            '<your reasons; a certifier or tie-break REJECT or objection on reasoning then adds a line '
            '"Proposed-repair:" and the repair fields>' % (record_id if record_id is not None else '<record id>'))


def _ref_id(value):
    m = re.fullmatch(r'\s*(?:#|record\s*#?|board\s*:\s*)?(\d+)\b.*', str(value or ''), re.I | re.S)
    return int(m.group(1)) if m else None


POST_EXAMPLE = ('Example of a valid post: board_call post {"text": "Post-type: reasoning\\nStatus: unverified\\nTask: bound for n=3\\n'
                'Claim: the map is injective for n=3.\\nEvidence: exhaustive check in EXPERIMENTS.md"}')


def text_field(value, limit=16000):
    # Operator ruling 2026-10-06: accept any value; reject only empty or oversize,
    # with the exact reason and an example.
    if value is None:
        value = ''
    if not isinstance(value, str):
        value = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    if not value.strip():
        raise BoardError("empty text. " + POST_EXAMPLE)
    size = len(value.encode())
    if size > limit:
        raise BoardError("text is %d bytes; the limit is %d. Split it into several posts or put the detail in "
                         "NOTES.md and post a summary. %s" % (size, limit, POST_EXAMPLE))
    return value


def positive_int(value):
    if type(value) is not int or value <= 0:
        raise BoardError("positive integer required")
    return value


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise BoardError("finite timestamp required")
    return value


class Board:
    """A trusted service object. Every public operation authenticates a capability.

    Controller methods use the launch-pinned controller secret. Worker methods
    additionally bind arm/problem, worker identity, role, and session generation.
    Reopening cannot replace a controller secret, arm, problem or review policy.
    """

    def __init__(self, path, *, arm_id, problem_id, controller_secret, mixed=False,
                 clock=time.time, cohort_id=None, expected_contracts_sha256=None, planning_state_path=None):
        self.path = Path(path).absolute()
        self.planning_state_path = planning_state_path
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise BoardError("symlink board path")
        self.arm_id, self.problem_id = identifier(arm_id), identifier(problem_id)
        self.cohort_id = identifier(cohort_id if cohort_id is not None else problem_id)
        self.contracts_hash = contracts_sha256()
        load_contracts(expected_sha256=expected_contracts_sha256 or self.contracts_hash)
        self.clock = clock
        if not isinstance(controller_secret, str) or len(controller_secret) < 32:
            raise BoardError("controller secret must have at least 32 characters")
        self._controller_hash = digest(controller_secret.encode())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise BoardError("board path must be a regular file")
        else:
            os.close(fd)
        self.path.chmod(0o600)
        with self._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workers(
                    id TEXT PRIMARY KEY, model TEXT NOT NULL, family TEXT NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'research',
                    initial_record INTEGER);
                CREATE TABLE IF NOT EXISTS sessions(
                    hash TEXT PRIMARY KEY, worker TEXT NOT NULL, epoch INTEGER NOT NULL,
                    role TEXT NOT NULL, pair_id INTEGER);
                CREATE TABLE IF NOT EXISTS artifacts(
                    hash TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    author TEXT NOT NULL, created REAL NOT NULL, body TEXT NOT NULL,
                    artifact TEXT, deps TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS obligations(
                    id TEXT PRIMARY KEY, lineage TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS leases(
                    key TEXT PRIMARY KEY, obligation TEXT NOT NULL, method TEXT NOT NULL,
                    deps TEXT NOT NULL, owner TEXT NOT NULL, version INTEGER NOT NULL,
                    updated REAL NOT NULL, deadline REAL NOT NULL, active_until REAL,
                    cost INTEGER NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assignments(
                    key TEXT NOT NULL, version INTEGER NOT NULL, owner TEXT NOT NULL,
                    PRIMARY KEY(key,version));
                CREATE TABLE IF NOT EXISTS pairs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate INTEGER NOT NULL,
                    candidate_hash TEXT NOT NULL, reviewer1 TEXT NOT NULL,
                    reviewer2 TEXT NOT NULL, deadline REAL NOT NULL,
                    state TEXT NOT NULL, purpose TEXT NOT NULL, exposure TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(
                    pair_id INTEGER NOT NULL, reviewer TEXT NOT NULL, body TEXT NOT NULL,
                    committed REAL NOT NULL, PRIMARY KEY(pair_id,reviewer));
                CREATE TABLE IF NOT EXISTS invalidations(
                    cause TEXT PRIMARY KEY, root INTEGER NOT NULL, reason TEXT NOT NULL,
                    active INTEGER NOT NULL, permanent INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS review_scopes(
                    worker TEXT PRIMARY KEY, record_id INTEGER NOT NULL, pages TEXT NOT NULL,
                    reads TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS digest_reads(
                    worker TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS member_roles(
                    worker TEXT PRIMARY KEY, role TEXT NOT NULL);
            ''')
            # executescript commits its caller's transaction. Reacquire the write
            # lock before binding identity/configuration and installing triggers.
            db.execute("BEGIN IMMEDIATE")
            # Immutable scientific rows cannot be changed even by accidental SQL updates.
            for table in ("records", "artifacts", "reviews", "assignments", "obligations"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'immutable scientific record'); END''')
            expected = {"schema": str(SCHEMA_VERSION), "arm": self.arm_id,
                        "problem": self.problem_id, "controller": self._controller_hash,
                        "mixed": str(bool(mixed)), "cohort": self.cohort_id,
                        "contracts_sha256": self.contracts_hash}
            for key, value in expected.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
                if db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0] != value:
                    raise BoardError("board identity or configuration mismatch")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('mode','research')")
        self.path.chmod(0o600)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _control(self, secret):
        if not isinstance(secret, str) or not hmac.compare_digest(
                digest(secret.encode()), self._controller_hash):
            raise BoardError("unauthorized")

    def _mode(self, db):
        return db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0]

    def _mutable(self, db, research=False):
        mode = self._mode(db)
        if mode == "frozen" or (research and mode != "research"):
            raise BoardError("board writes fenced")

    def _auth(self, db, token, arm_id, problem_id, roles):
        if arm_id != self.arm_id or problem_id != self.problem_id or not isinstance(token, str):
            raise BoardError("unauthorized")
        row = db.execute('''SELECT s.*, w.model, w.family, w.initial_record,
                            w.epoch AS current_epoch, w.role AS current_role
                            FROM sessions s JOIN workers w ON s.worker=w.id
                            WHERE s.hash=?''', (digest(token.encode()),)).fetchone()
        if (row is None or row["epoch"] != row["current_epoch"] or
                row["role"] != row["current_role"] or row["role"] not in roles):
            raise BoardError("unauthorized")
        return row

    def _session(self, db, worker, role, pair=None):
        db.execute("UPDATE workers SET epoch=epoch+1, role=? WHERE id=?", (role, worker))
        row = db.execute("SELECT epoch FROM workers WHERE id=?", (worker,)).fetchone()
        if row is None:
            raise BoardError("unknown worker")
        token = secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                   (digest(token.encode()), worker, row[0], role, pair))
        return token

    def register_worker(self, secret, worker_id, model, role=None):
        """role (optional, e.g. 'ideas', 'critic', 'literature'): the member's roster role,
        stamped as body field 'role' on its claim/finding/comment records."""
        self._control(secret)
        worker_id = identifier(worker_id)
        model = MODEL_KEYS.get(model, model)
        if model not in FAMILIES:
            raise BoardError("model has no board worker role")
        with self._transaction() as db:
            self._mutable(db, research=True)
            if db.execute("SELECT 1 FROM workers WHERE id=?", (worker_id,)).fetchone():
                raise BoardError("worker already registered")
            if db.execute("SELECT value FROM meta WHERE key='mixed'").fetchone()[0] == 'False' and model != 'DSF':
                raise BoardError("DSF-only board")
            db.execute("INSERT INTO workers(id,model,family) VALUES (?,?,?)",
                       (worker_id, model, FAMILIES[model]))
            if role:
                db.execute("INSERT OR REPLACE INTO member_roles VALUES (?,?)", (worker_id, str(role)))
            return self._session(db, worker_id, "research")

    def _member_role(self, db, worker):
        row = db.execute("SELECT role FROM member_roles WHERE worker=?", (worker,)).fetchone()
        return row[0] if row else None

    def issue_session(self, secret, worker_id, role="research"):
        self._control(secret)
        if role not in {"research", "report"}:
            raise BoardError("review sessions are issued with their pair")
        with self._transaction() as db:
            self._mutable(db, research=role == "research")
            if db.execute("SELECT 1 FROM pairs WHERE state='sealed' AND (reviewer1=? OR reviewer2=?)",
                          (worker_id, worker_id)).fetchone():
                raise BoardError("worker has a sealed review")
            return self._session(db, worker_id, role)

    def client(self, token):
        return BoardClient(self, token, self.arm_id, self.problem_id)

    def _record(self, db, record_id):
        positive_int(record_id)
        row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise BoardError("unknown record")
        return dict(row)

    def _deps(self, db, deps, require_valid=True):
        if not isinstance(deps, (tuple, list)) or len(deps) > MAX_DEPENDENCIES:
            raise BoardError("invalid dependencies")
        result = sorted(set(positive_int(x) for x in deps))
        for ref in result:
            self._record(db, ref)
            if require_valid and not self._valid(db, ref):
                raise BoardError("invalid dependency")
        return result

    def _ancestors(self, db, record_id):
        seen, pending = set(), [record_id]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(json.loads(self._record(db, current)["deps"]))
        return seen

    def _valid(self, db, record_id):
        ancestors = self._ancestors(db, record_id)
        return not any(row[0] in ancestors for row in db.execute(
            "SELECT root FROM invalidations WHERE active=1"))

    def _append(self, db, kind, author, body, *, artifact=None, deps=()):
        if kind in ("claim", "finding", "comment") and isinstance(body, dict) and "role" not in body:
            role = self._member_role(db, author)
            if role:
                body = dict(body, role=role)
        if kind in ("claim", "finding", "comment") and isinstance(body, dict) and "post_type" not in body:
            from .claim_ledger import _fields, legacy_verdict
            fields = _fields(body.get('statement') or body.get('text') or '')
            status = body.get('raw_status') or body.get('status') or fields.get('status')
            review = legacy_verdict(status) and (body.get('re') not in (None, '', '-') or fields.get('re')
                                               or str(author).startswith('critic:'))
            body = dict(body, implicit_type=True, post_type='verdict' if review else 'note')
        if kind in ('claim','finding','comment') and body.get('post_type')=='literature':
            from .literature_evidence import authenticate
            body=dict(body,literature_evidence_required=bool(getattr(self,'require_evidence',False)),
                      literature_evidence_verified=authenticate(body.get('literature'),getattr(self,'source_cache_root',None)))
            if body['literature_evidence_verified']:
                from .literature_evidence import retained_sources
                source_artifacts=[]
                for source in retained_sources(body.get('literature'),self.source_cache_root):
                    raw=('SOURCE FILE: '+source['location']+'\n'+source['text']).encode()
                    source_artifacts.extend(self._artifact(db,raw[i:i+MAX_ARTIFACT_BYTES]) for i in range(0,len(raw),MAX_ARTIFACT_BYTES))
                body=dict(body,source_artifacts=source_artifacts)
        scope=db.execute('SELECT * FROM review_scopes WHERE worker=?',(author,)).fetchone()
        commit_review=False
        if scope and kind in ('finding','claim','comment') and body.get('post_type')=='verdict':
            from .claim_ledger import _fields, verdict_word, _ref
            fields=_fields(body.get('statement') or body.get('text') or '')
            status=verdict_word(body.get('raw_status') or body.get('status') or fields.get('status'))
            target=_ref(body.get('re') or fields.get('re'))
            # A bound session reviews exactly one revision: a verdict without Re is about it. A
            # certifier's or tie-breaker's verdict naming another record is refused with the exact form
            # (the claim ledger would keep it as stale and count nothing; operator 2026-10-11). A pair
            # critic's late objection to a newer revision stays admitted as provenance.
            from .claim_ledger import is_cert, is_tiebreak
            if target is None:target=scope['record_id']
            if status and target!=scope['record_id'] and (is_cert(author) or is_tiebreak(author)):
                raise BoardError('verdict requires Re: %d, the revision this session reviews (your post says Re: %s). '
                                 'Repost it with these first lines:\n%s' % (scope['record_id'], target, verdict_form(scope['record_id'])))
            if status and target==scope['record_id']:
                missing=sorted(set(range(len(json.loads(scope['pages']))))-set(json.loads(scope['reads'])))
                if status=='accept' and missing:
                    raise BoardError('incomplete evidence coverage: read every review_page before acceptance; not yet read: '
                                     'pages %s. Call board_call review_page {"page": N} for each, then repost the same verdict.'
                                     % ', '.join(map(str, missing)))
                commit_review=True
        if body.get('post_type')=='reasoning':
            deps=sorted(set(deps)|set(body.get('premise_refs') or [])|set(body.get('literature_refs') or []))
        encoded = canonical(body)
        if len(encoded.encode()) > MAX_RECORD_BYTES:
            raise BoardError("record too large; publish an artifact")
        cursor = db.execute("INSERT INTO records(kind,author,created,body,artifact,deps) VALUES (?,?,?,?,?,?)",
                            (kind, author, finite(self.clock()), encoded, artifact, canonical(list(deps))))
        if commit_review:
            db.execute('UPDATE review_scopes SET committed=1 WHERE worker=?',(author,))
        return cursor.lastrowid

    def _artifact(self, db, content):
        if not isinstance(content, bytes) or not content or len(content) > MAX_ARTIFACT_BYTES:
            raise BoardError("invalid artifact size")
        hashed = digest(content)
        db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?)", (hashed, content))
        return hashed

    def _visible(self, auth):
        pass  # row visibility is checked below, across every evidence API

    def _review_visible(self, db, auth, row):
        scope=db.execute('SELECT * FROM review_scopes WHERE worker=?',(auth['worker'],)).fetchone()
        if scope and not scope['committed'] and row['author'] != auth['worker']:
            body=json.loads(row['body'])
            if row['kind'] not in ('claim','finding','comment') or body.get('post_type') not in ('reasoning','literature'):
                raise BoardError('peer decisions withheld until first verdict')

    def bind_review(self, secret, *, worker, record_id, text, deliver_bytes=0, checked_sources=False):
        """Bind a review session to one revision's evidence pages. deliver_bytes: the harness puts the
        leading whole pages up to that size in the session's brief and records them as read (the
        delivery is the read receipt; v2/cert_bundle). checked_sources: the text already carries the
        K3-checked literature reports it cites, so their retained source bytes are not required reading
        (the bound record's own sources always are)."""
        self._control(secret)
        with self._transaction() as db:
            row=self._record(db,record_id)
            evidence=text.encode()
            # All referenced artifact bytes enter the review pages, including
            # continuation/evidence content retained by the controller.
            ancestors=self._ancestors(db,record_id)
            artifacts=set()
            for r in db.execute('SELECT * FROM records'):
                if r['id'] in ancestors:
                    if r['artifact']:artifacts.add(r['artifact'])
                    if not checked_sources or r['id']==record_id:artifacts.update(json.loads(r['body']).get('source_artifacts') or [])
            for artifact in sorted(artifacts):
                data=db.execute('SELECT content FROM artifacts WHERE hash=?',(artifact,)).fetchone()[0]
                evidence += b'\nARTIFACT '+artifact.encode()+b'\n'+bytes(data)
            pages=[]; start=0
            while start<len(evidence):
                end=min(start+12000,len(evidence))
                # Keep UTF-8 code points intact across evidence pages.
                while end<len(evidence) and end>start and evidence[end]&0xc0==0x80:end-=1
                if end==start:end=min(start+12000,len(evidence))
                pages.append(self._artifact(db,evidence[start:end]));start=end
            pages=pages or [self._artifact(db,b'empty evidence')]
            delivered,size=[],0
            for h in pages:
                content=bytes(db.execute('SELECT content FROM artifacts WHERE hash=?',(h,)).fetchone()[0])
                if size+len(content)>deliver_bytes:break
                delivered.append(content);size+=len(content)
            db.execute('INSERT OR IGNORE INTO review_scopes(worker,record_id,pages,reads) VALUES (?,?,?,?)',
                       (worker,record_id,canonical(pages),canonical(list(range(len(delivered))))))
            existing=db.execute('SELECT record_id,pages FROM review_scopes WHERE worker=?',(worker,)).fetchone()
            if existing[0]!=record_id or json.loads(existing[1])!=pages:raise BoardError('review scope changed')
            return dict(record_id=record_id,pages=len(pages),page_hashes=pages,delivered=len(delivered),
                        delivered_text=b''.join(delivered).decode(errors='replace'))

    def review_page(self, token, arm_id, problem_id, *, page=0):
        with self._transaction() as db:
            auth=self._auth(db,token,arm_id,problem_id,{'research'})
            scope=db.execute('SELECT * FROM review_scopes WHERE worker=?',(auth['worker'],)).fetchone()
            if not scope:raise BoardError('no bound evidence view')
            pages=json.loads(scope['pages'])
            if type(page) is not int or not 0<=page<len(pages):raise BoardError('missing review page')
            content=db.execute('SELECT content FROM artifacts WHERE hash=?',(pages[page],)).fetchone()[0]
            if digest(content)!=pages[page]:raise BoardError('review page integrity failure')
            reads=set(json.loads(scope['reads']));reads.add(page)
            db.execute('UPDATE review_scopes SET reads=? WHERE worker=?',(canonical(sorted(reads)),auth['worker']))
            return dict(record_id=scope['record_id'],page=page,pages=len(pages),sha256=pages[page],content=bytes(content).decode(errors="replace"))

    def _public(self, db, row):
        item = dict(row)
        item["body"] = json.loads(item["body"])
        item["deps"] = json.loads(item["deps"])
        item["valid"] = self._valid(db, item["id"])
        return item

    def post_initial(self, token, arm_id, problem_id, *, approach, assumptions, target, content):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            if auth["initial_record"] is not None:
                raise BoardError("initial proposal already committed")
            body = {"approach": text_field(approach), "assumptions": text_field(assumptions),
                    "target": identifier(target)}
            hashed = self._artifact(db, content)
            record = self._append(db, "strategy", auth["worker"], body, artifact=hashed)
            db.execute("UPDATE workers SET initial_record=? WHERE id=?", (record, auth["worker"]))
            return {"record_id": record, "artifact_hash": hashed}

    # ---- open-swarm notes (operator ~18:15 PDT): claims, findings, comments ----
    CLAIM_STALE_SECONDS = 1200
    STATUSES = ('verified', 'computation-checked', 'unverified', 'refuted', 'disputed')
    CRITICS = ('K3', 'Qwen')

    def _verdict_fields(self, auth, raw_status):
        """Redesign item 1: the board stores the raw status and the critic's verdict as
        posted; the claim ledger (v2/claim_ledger.py) computes accepted/refuted/disputed."""
        from .claim_ledger import verdict_word
        verdict = verdict_word(raw_status)
        critic = auth['model'] in self.CRITICS
        return {"raw_status": str(raw_status or '')[:200], "model": auth['model'],
                "critic": auth['model'] if critic and verdict == 'reject' else None,
                "reviewer_model": auth['model'] if critic else None, "verdict": verdict}

    def member_roles(self, secret):
        """{worker: role} for roster members (controller only)."""
        self._control(secret)
        with self._transaction() as db:
            try:return {r[0]: r[1] for r in db.execute("SELECT worker, role FROM member_roles")}
            except Exception:return {}

    def import_claims(self, secret, items):
        """Continuation start (v2/claim_import.py): post each earlier session's unsettled claim
        once, as controller records (the claim, then continuation parts when its text does not
        fit one record). Idempotent by origin; an item that cannot be posted is skipped and
        reported, never the batch. Returns ({origin: record id}, [(origin, error)])."""
        from .claim_import import bodies, part_body
        self._control(secret)
        fits = lambda b: len(canonical(b).encode()) <= MAX_RECORD_BYTES - 512
        have, errors = {}, []
        with self._transaction() as db:
            for row in db.execute("SELECT id, body FROM records WHERE author='controller' AND kind='finding'"):
                b = json.loads(row["body"])
                o = b.get("imported", {}).get("origin") if isinstance(b, dict) and isinstance(b.get("imported"), dict) else None
                if isinstance(o, str):
                    have[o] = row["id"]
        for i in items or ():
            try:
                if not isinstance(i, dict) or not isinstance(i.get("origin"), str) or not i["origin"]:
                    raise ValueError("import without an origin")
                if i["origin"] in have:
                    continue
                head, parts = bodies(i, fits)
                with self._transaction() as db:
                    rid = self._append(db, "finding", "controller", head)
                    for k, t in enumerate(parts, 2):
                        self._append(db, "comment", "controller", part_body(t, rid, k, len(parts) + 1))
                have[i.get("origin")] = rid
            except Exception as exc:
                errors.append((str(i.get("origin") if isinstance(i, dict) else i)[:300], type(exc).__name__ + ': ' + str(exc)[:200]))
        return have, errors

    def ledger(self, secret, policy=None):
        """Claim ledger from the board snapshot (controller only). policy: as
        claim_ledger.load_policy; None = roles/<arm_id>/policy.json or defaults."""
        snap = json.loads(self.export_snapshot(secret)["snapshot"])
        with self._transaction() as db:
            return self._ledger_of(db, snap["records"], policy)

    def _ledger_of(self, db, records, policy=None):
        from .claim_ledger import ledger, load_policy
        models = {r["id"]: r["model"] for r in db.execute("SELECT id,model FROM workers")}
        roles = {r[0]: r[1] for r in db.execute("SELECT worker, role FROM member_roles")}
        policy = policy if policy is not None else load_policy(self.arm_id)
        if roles:
            # Roster members carry their role; then critics are the registered critic members
            # (plus critic_workers), never identified by model: an ideas member whose model is
            # listed in critic_models is not a critic, even before any critic has posted.
            from .claim_ledger import CRITIC_ROLES
            critics = sorted(w for w, r in roles.items() if str(r).lower() in CRITIC_ROLES)
            # The placeholder keeps the critic set non-empty before any critic is registered,
            # so the ledger's "nobody identifiable: anyone judges" fallback never applies here.
            # A worker without a registered role cannot self-label as critic ("Role: critic" in
            # its text): its records carry role 'none', which the ledger reads before the text.
            for rec in records:
                b = rec.get("body")
                if rec.get("author") not in roles and isinstance(b, dict):
                    rec["body"] = dict(b, role="none")
                elif rec.get("author") in roles and isinstance(b, dict):
                    rec["body"] = dict(b, role=roles[rec["author"]])
            policy = dict(policy, critic_models=[],
                          critic_workers=list(dict.fromkeys(list(policy.get('critic_workers') or []) + critics
                                                            + ['(no critic registered)'])))
        return ledger(records, policy, models)

    def _restatement(self, db, kind, author, body):
        """Duplicate-claim rule (operator 2026-10-07): when this post would only restate an existing
        claim with no new content (or restate a refuted claim without addressing the refutation),
        return the reply the proposer gets instead of a new record; else None. Any doubt or error
        posts it (never a refusal on format)."""
        try:
            records = [dict(id=r["id"], kind=r["kind"], author=r["author"], created=r["created"], body=json.loads(r["body"]))
                       for r in db.execute("SELECT id,kind,author,created,body FROM records "
                                           "WHERE kind IN ('finding','claim','comment') ORDER BY id")]
            nid = (db.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0] or 0) + 1
            role = self._member_role(db, author)
            records.append(dict(id=nid, kind=kind, author=author, created=self.clock(),
                                body=dict(body, role=role) if role and "role" not in body else body))
            claims = self._ledger_of(db, records)
        except Exception:
            return None
        for c in claims.values():
            hit = next((x for x in c.get('restatements') or () if x.get('record') == nid), None)
            if hit:
                rid = (c['revisions'][-1]['record'] if c.get('revisions') else None) or c['records']['proposal']
                state = hit.get('state') or c.get('state')
                why = ('That claim is refuted; a repair must say how it answers the refutation (cite the rejecting '
                       'verdict or the counterexample and what changed). Post the changed text with '
                       '"Post-type: reasoning" and "Revision-of: #%d" in the first lines.' % rid if state == 'refuted' else
                       'It adds no new content, so it was not posted. To repair the claim, post the changed text with '
                       '"Post-type: reasoning" and "Revision-of: #%d" in the first lines; otherwise the critics\' verdicts on #%d stand.' % (rid, rid))
                return dict(posted=False, duplicate_of=rid, claim_state=state,
                            note='Not posted: this restates existing claim #%d (current state: %s). %s' % (rid, state, why))
        return None

    @staticmethod
    def _task_key(task):
        text = ' '.join(str(task).lower().split())
        return re.sub(r'^[*_`~\s]+|[*_`~\s]+$', '', text)[:300]

    def _open_claims(self, db):
        claims = {}
        for row in db.execute("SELECT id,kind,author,created,body FROM records WHERE kind IN ('claim','finding','release') ORDER BY id"):
            body = json.loads(row['body']); key = body.get('task_key')
            if row['kind'] == 'claim':
                claims[key] = dict(worker=row['author'], at=row['created'], task=body.get('task'), record=row['id'])
            elif key in claims and claims[key]['worker'] == row['author']:
                claims.pop(key)
        now = self.clock()
        return {k: v for k, v in claims.items() if now - v['at'] < self.CLAIM_STALE_SECONDS}

    def _light(self, db, auth):
        # Open swarm: the GLM member is a light member (comments only) unless it was
        # registered with a roster role.
        return self.arm_id == 'open-swarm' and auth['model'] == 'GLM' and not self._member_role(db, auth['worker'])

    def _not_light(self, auth, db=None):
        if self.arm_id == 'open-swarm' and auth['model'] == 'GLM' and not (db is not None and self._member_role(db, auth['worker'])):
            raise BoardError("light member: post_comment only")

    def post_claim(self, token, arm_id, problem_id, *, task, plan='', help_needed=''):
        """Announce what this member is working on. A live claim on the same task by
        another member is rejected; claims end at a finding/release or after 20 min."""
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            self._not_light(auth, db)
            key = self._task_key(task)
            if not key:raise BoardError("task required")
            live = self._open_claims(db).get(key)
            if live and live['worker'] != auth['worker']:
                raise BoardError("duplicate claim: " + live['worker'] + " is working on this task")
            return self._append(db, "claim", auth["worker"], {"task": text_field(task), "task_key": key,
                "plan": text_field(plan or '-'), "help_needed": text_field(help_needed or '-')})

    STATUS_WORDS = {'verified': 'verified', 'proved': 'verified', 'proven': 'verified', 'confirmed': 'verified',
                    'computation-checked': 'computation-checked', 'computed': 'computation-checked', 'checked': 'computation-checked',
                    'numerically-checked': 'computation-checked', 'unverified': 'unverified', 'refuted': 'refuted',
                    'disproved': 'refuted', 'false': 'refuted', 'reject': 'refuted', 'rejected': 'refuted',
                    'disputed': 'disputed', 'accept': 'unverified', 'accepted': 'unverified'}

    @classmethod
    def normalize_status(cls, status):
        """Review 8e7c704 #4: the first word only, matched exactly; anything else is unverified."""
        words = re.findall(r"[a-z][a-z-]*", str(status or '').lower().replace('_', '-'))
        if not words:return 'unverified'
        first = words[0]
        if first == 'computation' and len(words) > 1 and words[1].startswith('check'):first = 'computation-checked'
        return cls.STATUS_WORDS.get(first, 'unverified')

    def _literature_links(self, db, post_type, refs):
        if refs is None: return
        if post_type != 'reasoning' or not isinstance(refs,list) or any(type(i) is not int for i in refs):
            raise BoardError('literature_refs requires exact source revisions on reasoning claims')
        records=[self._public(db,r) for r in db.execute('SELECT * FROM records ORDER BY id')]
        from .global_planner import current
        confirmed={current(c) for c in self._ledger_of(db,records).values() if c.get('post_type')=='literature' and c['state']=='accepted'}
        if any(i not in confirmed for i in refs): raise BoardError('literature_refs must cite K3-confirmed current literature revisions')

    def _premise_links(self, db, post_type, refs):
        if post_type!='reasoning':return
        if refs is None:
            if getattr(self,'require_evidence',False):raise BoardError('Premise-refs required: explicitly declare all reasoning/source revision dependencies, or [] for none')
            return
        if not isinstance(refs,list) or any(type(i) is not int for i in refs):raise BoardError('Premise-refs requires exact current revision IDs')
        from .global_planner import current
        records=[self._public(db,r) for r in db.execute('SELECT * FROM records ORDER BY id')]
        claims={current(c):c for c in self._ledger_of(db,records).values()}
        if any(i not in claims or claims[i]['state']!='accepted' for i in refs):raise BoardError('Premise-refs requires accepted current revisions')

    def _revision_link(self, db, revision_of):
        if revision_of is None: return
        records = [self._public(db, row) for row in db.execute("SELECT * FROM records WHERE kind IN ('finding','claim','comment') ORDER BY id")]
        claims = self._ledger_of(db, records)
        current = {(c['revisions'][-1]['record'] if c.get('revisions') else c['records']['proposal']) for c in claims.values()}
        if revision_of not in current:
            # turn audit 2026-10-11: name the current revision (e.g. 124 -> 468) instead of making the author search.
            claim = next((c for c in claims.values() if revision_of in [c['records']['proposal']] + [r['record'] for r in c.get('revisions') or []]), None)
            if claim is not None:
                cur = claim['revisions'][-1]['record'] if claim.get('revisions') else claim['records']['proposal']
                raise BoardError('Revision-of must link the exact current result revision: %d is an earlier revision of "%s"; '
                                 'its current revision is %d: use revision_of=%d' % (revision_of, str(claim['key'])[:80], cur, cur))
            row = db.execute('SELECT kind, body FROM records WHERE id=?', (revision_of,)).fetchone()
            if row is None:
                raise BoardError('Revision-of must link the exact current result revision: %d is not a record on this board' % revision_of)
            kind = (json.loads(row['body']) if row['body'] else {}).get('post_type') or row['kind']
            raise BoardError('Revision-of must link the exact current result revision, never a note or verdict: %d is a %s; pass '
                             'the current revision of the reasoning or literature claim you are changing' % (revision_of, kind))

    def _review_target(self, db, auth, post_type, status, target):
        """A bound review session's verdict without Re: is about its bound revision. Callers use
        the same resolved target for the verdict, the repair and its lineage links."""
        from .claim_ledger import is_cert, is_tiebreak, verdict_word
        if post_type is None and (is_cert(auth['worker']) or is_tiebreak(auth['worker'])) and verdict_word(status) in ('reject', 'object'):
            post_type = 'verdict'
        if target is not None or post_type != 'verdict': return target
        bound = db.execute('SELECT record_id FROM review_scopes WHERE worker=?', (auth['worker'],)).fetchone()
        return bound[0] if bound else None

    def _review_repair(self, db, auth, post_type, status, target, repair, objection):
        from .claim_ledger import is_cert, is_tiebreak, verdict_word
        from .global_planner import current
        from .certifier_repair import validate, required_families, reviewer_family, MAX_REPAIR_DEPTH
        reviewer = is_cert(auth['worker']) or is_tiebreak(auth['worker'])
        if reviewer and post_type is None and verdict_word(status) in ('reject', 'object'): post_type = 'verdict'
        target = self._review_target(db, auth, post_type, status, target)
        if reviewer and post_type == 'verdict' and verdict_word(status) in ('reject', 'object') and target is None:
            raise BoardError('REJECT with repair requires Re: exact rejected revision record id. First lines:\n' + verdict_form())
        if not reviewer or post_type != 'verdict':
            if repair is not None: raise BoardError('Proposed-repair belongs to a certifier or tie-break REJECT verdict')
            return None
        records = [self._public(db, r) for r in db.execute('SELECT * FROM records ORDER BY id')]
        claim = next((c for c in self._ledger_of(db, records).values()
                      if target in [c['records']['proposal']] + [r['record'] for r in c.get('revisions', [])]), None)
        # Concurrent reviewers can reject the same frozen revision. Preserve a later
        # repair as an alternative lineage, never replace the first reviewer's new text.
        if verdict_word(status) not in ('reject', 'object') or claim is None or claim.get('post_type') == 'literature':
            if repair is not None: raise BoardError('repair requires REJECT of an exact reasoning revision')
            return None
        prose = re.sub(r'^\s*(?:Post-type|Status|Re|Task|Claim|Evidence|Verified-by):[^\n]*$', '', objection, flags=re.M | re.I).strip()
        if not prose or not prose.removeprefix('Objection:').strip():
            raise BoardError('REJECT requires an objection as well as the proposed repair')
        scope = db.execute('SELECT * FROM review_scopes WHERE worker=?', (auth['worker'],)).fetchone()
        if scope:
            if scope['record_id'] != target: raise BoardError('repair must reject the session bound revision: verdict requires Re: %d. First lines:\n%s' % (scope['record_id'], verdict_form(scope['record_id'])))
            if set(json.loads(scope['reads'])) != set(range(len(json.loads(scope['pages'])))):
                raise BoardError('incomplete evidence coverage: read every review_page before proposing a repair; not yet read: pages %s'
                                 % ', '.join(map(str, sorted(set(range(len(json.loads(scope['pages'])))) - set(json.loads(scope['reads']))))))
        repair_hash = digest(repair.strip().encode()) if isinstance(repair, str) else None
        for r in records:
            body = r.get('body') or {}
            if r.get('author') == auth['worker'] and body.get('repair_of') == target and body.get('repair_sha256') == repair_hash:
                return dict(existing_reply=dict(verdict_record=body['repair_verdict'],repair_record=r['id'],
                    repair_records=list(range(r['id'],r['id']+body.get('parts',1))),
                    repair_alternative=body.get('repair_alternative',False),duplicate=True))
        if is_cert(auth['worker']) and reviewer_family(auth['worker'], auth['model']) not in required_families(claim):
            raise BoardError('certifier family is ineligible to mutate the repair text it wrote')
        if claim.get('repair_depth', 0) >= MAX_REPAIR_DEPTH:
            if verdict_word(status) != 'reject' or repair is not None:
                raise BoardError('repair lineage exhausted: post terminal REJECT with objection and no Proposed-repair')
            return None
        try: validate(repair)
        except ValueError as exc: raise BoardError(str(exc)) from exc
        return claim

    def _append_review_repair(self, db, auth, claim, target, verdict, repair):
        from .global_planner import current
        from .certifier_repair import validate, reviewer_family
        fields = validate(repair)
        self._premise_links(db, 'reasoning', fields.get('premise-refs'))
        self._literature_links(db, 'reasoning', fields.get('literature-refs'))
        task = fields['corrected-statement'][:2000]
        alternative = current(claim) != target
        text = ('Post-type: reasoning\nStatus: unverified\nRevision-of: #%s\nTask: %s\n'
                'Repair of rejection record #%s; addresses the objection with the following proposal.\n%s'
                % (target, task, verdict, repair))
        if len(text.encode()) > self.POST_LIMIT: raise BoardError('repair exceeds board post limit')
        chunks = self._chunks(text, first=40000)
        rid = self._append(db, 'finding', auth['worker'], dict(post_type='reasoning',
            revision_of=target, repair_of=target, repair_verdict=verdict,
            repair_alternative=alternative, repair_sha256=digest(repair.strip().encode()),
            repair_complete=bool(fields.get('corrected-proof')),
            repair_depth=claim.get('repair_depth', 0) + 1,
            repair_author_families=sorted(set(claim.get('repair_author_families') or []) |
                {reviewer_family(auth['worker'], auth['model'])}),
            premise_refs=fields.get('premise-refs',[]),literature_refs=fields.get('literature-refs',[]),
            task=task, task_key=('repair:%s:%s' % (target, verdict)) if alternative else claim['key'], status='unverified', raw_status='unverified',
            model=auth['model'], statement=chunks[0], parts=len(chunks)))
        ids = [rid]
        for n, chunk in enumerate(chunks[1:], 2):
            ids.append(self._append(db, 'comment', auth['worker'], dict(post_type='reasoning', text=chunk,
                about='continued from record %d (part %d of %d)' % (rid, n, len(chunks)))))
        return dict(verdict_record=verdict, repair_record=rid, repair_records=ids, repair_alternative=alternative, repair_sha256=digest(repair.strip().encode()),
                    note='Repair posted as an unverified reasoning revision; fresh review required')

    def _require_revision(self, db, post_type, revision_of, task, text):
        if post_type not in ('reasoning', 'literature') or revision_of is not None:
            return
        from .claim_ledger import same_content
        records = [self._public(db, row) for row in db.execute("SELECT * FROM records WHERE kind IN ('finding','claim','comment') ORDER BY id")]
        for c in self._ledger_of(db, records).values():
            if c['key'] == self._task_key(task) and not same_content(c['text'], text):
                cur = c['revisions'][-1]['record'] if c.get('revisions') else c['records']['proposal']
                raise BoardError('Revision-of required to change an existing result; link its exact current revision: result %d '
                                 '(state %s) already has different text under this task. To change it pass revision_of=%d; '
                                 'for a separate result use a different task' % (cur, c.get('state'), cur))

    def post_finding(self, token, arm_id, problem_id, *, task='', status='unverified', statement='', evidence='', verified_by='', post_type=None, revision_of=None, literature=None, re=None, literature_refs=None, premise_refs=None, proposed_repair=None):
        """Record a result with its verification status; releases this member's claim."""
        from .post_types import validate
        from .claim_ledger import _fields
        line = _fields(statement).get('revision-of')
        if line is not None:
            # turn audit 2026-10-11: 11 of 12 such refusals carried the same id in both places, as the posting
            # rules ask for the line; a matching line is redundant and is dropped.
            m = __import__('re').search(r'\d+', str(line)); n = int(m.group()) if m else None
            if n is not None and str(revision_of).strip().isdigit() and n == int(str(revision_of).strip()):
                statement = __import__('re').sub(r'(?im)^[ \t]*Revision-of[ \t]*:[^\n]*\n?', '', statement, count=1)
            else:
                raise BoardError('Revision-of must be supplied as the structured revision_of argument: pass revision_of=%s and '
                                 'delete the line "Revision-of: %s" from statement' % (n if n is not None else '<current record id>', str(line)[:40]))
        if isinstance(literature, str):
            from .format_json import loads
            try: literature = loads(literature)  # JSON presentation only
            except (ValueError, SyntaxError): pass
        if post_type is None and __import__('re').search(r'^\s*Post-type\s*:', statement, __import__('re').I | __import__('re').M):
            raise BoardError('Post-type must be declared in the structured post')
        if post_type is not None:
            try: validate(post_type, statement, literature)
            except ValueError as exc: raise BoardError(str(exc)) from exc
        if revision_of is not None: positive_int(revision_of)
        if post_type == 'verdict' and re is None: raise BoardError('verdict requires Re: exact revision record id')
        if re is not None: positive_int(re)
        raw_status = status
        statement = statement or task
        task = task or str(statement)[:200]
        evidence = evidence or '-'
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            self._not_light(auth, db)
            self._revision_link(db, revision_of)
            self._require_revision(db, post_type, revision_of, task, statement + '\n' + evidence)
            self._literature_links(db, post_type, literature_refs)
            self._premise_links(db,post_type,premise_refs)
            if post_type == 'literature' and auth['model'] != 'DSF':
                raise BoardError('literature source reports must be authored by Flash')
            repair_target = self._review_target(db, auth, post_type, raw_status, re)
            repair_claim = self._review_repair(db, auth, post_type, raw_status, re, proposed_repair, statement + '\n' + (evidence if evidence != '-' else ''))
            if repair_claim and repair_claim.get('existing_reply'): return repair_claim['existing_reply']
            rid = self._append(db, "finding", auth["worker"], {"task": text_field(task), "task_key": self._task_key(task),
                "status": self.normalize_status(raw_status), **self._verdict_fields(auth, raw_status),
                "statement": text_field(statement), "evidence": text_field(evidence),
                "verified_by": text_field(verified_by or '-'),
                **({'post_type': post_type} if post_type else {}), **({'re': re} if re is not None else {}),
                **({'revision_of': revision_of} if revision_of is not None else {}),
                **({'literature': literature} if literature is not None else {}),
                **({'literature_refs': literature_refs} if literature_refs is not None else {}),
                **({'premise_refs':premise_refs} if premise_refs is not None else {})})
            if repair_claim is not None:
                return self._append_review_repair(db, auth, repair_claim, repair_target, rid, proposed_repair)
            return rid

    def release_claim(self, token, arm_id, problem_id, *, task, reason=''):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            return self._append(db, "release", auth["worker"], {"task_key": self._task_key(task), "reason": text_field(reason or '-')})

    POST_LIMIT = 240000
    CHUNK_BYTES = 48000

    @staticmethod
    def _label_value(value):
        return re.sub(r'^[\s*_`]+|[\s*_`]+$', '', value)

    def post(self, token, arm_id, problem_id, *, text):
        """Markdown post (operator ruling 2026-10-06). Optional leading lines are read
        leniently: "Post-type: reasoning", "Status: ...", "Task: ...", "Claim: ...",
        "Release: ...", "Re: <id>", "Revision-of: <id>". Every result claim needs
        Post-type: reasoning or literature. Verdicts use Post-type: verdict and Re.
        An unlabelled post that is not a verdict is a plain note. Never refused for format; long posts are chunked."""
        return self._markdown(token, arm_id, problem_id, text, route=True)

    def _chunks(self, text, first=None):
        # Bound each record by its encoded size (JSON escaping included), not raw UTF-8.
        parts, cur, size = [], [], 0
        for ch in text:
            n = len(canonical(ch).encode()) - 2
            if size + n > (first if first is not None and not parts else self.CHUNK_BYTES) and cur:
                parts.append(''.join(cur)); cur, size = [], 0
            cur.append(ch); size += n
        if cur:parts.append(''.join(cur))
        return parts

    def _markdown(self, token, arm_id, problem_id, text, *, route):
        if text is None or (isinstance(text, str) and not text.strip()):
            raise BoardError("empty text. " + POST_EXAMPLE)
        if not isinstance(text, str):
            text = json.dumps(text, sort_keys=True, default=str)
        if len(text.encode()) > self.POST_LIMIT:
            raise BoardError("text is %d bytes; the limit is %d. Split it into several posts or put the detail in "
                             "NOTES.md and post a summary. %s" % (len(text.encode()), self.POST_LIMIT, POST_EXAMPLE))
        from .certifier_repair import split
        text, proposed_repair = split(text) if route else (text, None)
        fields = {}; raw_fields = {}
        label_pattern = r"^\s*[*#>_-]*\s*[*_]*\s*(status|task|claim|release|re|evidence|verified[ _-]?by|post-type|revision-of|literature-refs|literature_refs|premise-refs|premise_refs|literature)\s*[*_]*\s*[:=]\s*(.*)$"
        for line in text.splitlines()[:8] if route else []:
            m = re.match(label_pattern, line, re.I)
            value = self._label_value(m.group(2)) if m else ''
            if m and value:
                raw_fields.setdefault(m.group(1).lower().replace(' ', '').replace('_', '').replace('-', ''), value)
            if m and len(value) > 600:
                value = value[:600] + ' [truncated; full text in the post]'
            if m and value:
                fields.setdefault(m.group(1).lower().replace(' ', '').replace('_', '').replace('-', ''), value)
        post_type = raw_fields.get('posttype')
        if route and post_type == 'literature' and 'literature' not in raw_fields:
            # Use exactly the same Markdown labels at the end as in the header.
            for line in text.splitlines():
                m = re.match(label_pattern, line, re.I)
                if m and m.group(1).lower() == 'literature':
                    raw_fields['literature'] = self._label_value(m.group(2));break
        typed = {'post_type': 'note'} if not route else {}
        if post_type:
            from .post_types import validate
            try:
                from .format_json import loads, refs
                card = loads(raw_fields.get('literature', 'null'))
                report_lines = []
                for line in text.splitlines():
                    label = re.match(label_pattern, line, re.I)
                    key = label.group(1).lower().replace('-', '') if label else ''
                    if key not in ('posttype', 'literature', 'status', 'task', 'claim', 'revisionof', 're'):
                        report_lines.append(line)
                report = '\n'.join(report_lines).strip()
                validate(post_type, report, card)
                if post_type == 'verdict' and raw_fields.get('re') and _ref_id(raw_fields['re']) is None:
                    raise ValueError('verdict requires Re: <exact revision record id> as a number; got Re: %s. First lines:\n%s'
                                     % (raw_fields['re'][:80], verdict_form()))
                typed = {'post_type': post_type}
                if card is not None: typed['literature'] = card
                if 'premiserefs' in raw_fields: typed['premise_refs'] = refs(raw_fields['premiserefs'])
                if 'literaturerefs' in raw_fields: typed['literature_refs'] = refs(raw_fields['literaturerefs'])
                if 'revisionof' in raw_fields: typed['revision_of'] = positive_int(int(raw_fields['revisionof'].lstrip('#')))
            except (ValueError, TypeError, SyntaxError) as exc:
                raise BoardError(str(exc)) from exc
        # Review 8e7c704 #2: bound the whole first record (metadata included). Label
        # values are at most 2000 characters; each also stays in the full text.
        meta = len(canonical({k: v[:2000] for k, v in fields.items()}).encode()) + len(canonical(typed).encode()) + 1024
        first_budget = MAX_RECORD_BYTES - meta - 4096
        if first_budget < 1:
            raise BoardError('post metadata exceeds the record limit; shorten the literature card while retaining exact source ids, hashes and locations')
        parts = self._chunks(text, first=first_budget)
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            light = self._light(db, auth)
            self._revision_link(db, typed.get('revision_of'))
            self._literature_links(db, post_type, typed.get('literature_refs'))
            self._premise_links(db,post_type,typed.get('premise_refs'))
            if post_type == 'literature' and auth['model'] != 'DSF':
                raise BoardError('literature source reports must be authored by Flash')
            self._require_revision(db, post_type, typed.get('revision_of'), fields.get('task') or fields.get('claim') or next((l for l in text.splitlines() if l.strip()), ''), text)
            from .claim_ledger import _ref
            target = _ref(raw_fields.get('re'))
            if post_type == 'verdict' and target is None and not db.execute('SELECT 1 FROM review_scopes WHERE worker=?', (auth['worker'],)).fetchone():
                raise BoardError('verdict requires Re: <exact revision record id> in its first eight lines. First lines:\n' + verdict_form())
            repair_target = self._review_target(db, auth, post_type, fields.get('status'), target)
            repair_claim = self._review_repair(db, auth, post_type, fields.get('status'), target, proposed_repair, text)
            if repair_claim and repair_claim.get('existing_reply'): return repair_claim['existing_reply']
            first = next((self._label_value(l.strip('# ')) for l in text.splitlines() if l.strip()), '')[:200]
            head = parts[0]
            if not light and post_type in ('reasoning', 'literature') and len(parts) == 1 and ('status' in fields or ('claim' in fields and 'task' not in fields)):
                # Duplicate-claim rule: a pure restatement of an existing claim is not posted.
                probe = dict(task=(fields.get('task') or fields.get('claim') or first)[:2000], statement=text,
                             task_key=self._task_key(raw_fields.get('task') or raw_fields.get('claim') or first),
                             raw_status=fields.get('status', ''), status=fields.get('status', ''), **typed, re=fields.get('re', '-')[:200],
                             evidence=fields.get('evidence', '-')[:2000], model=auth['model'])
                if 'status' not in fields:
                    probe = dict(task=fields['claim'][:2000], task_key=self._task_key(raw_fields['claim']), plan=text)
                dup = self._restatement(db, "finding" if 'status' in fields else "claim", auth["worker"], probe)
                if dup is not None:
                    return dup
            if not light and ('status' in fields or post_type in ('reasoning', 'literature')) and post_type != 'note':
                task = fields.get('task') or fields.get('claim') or first
                key_src = raw_fields.get('task') or raw_fields.get('claim') or first
                rid = self._append(db, "finding", auth["worker"], {"task": task[:2000], "task_key": self._task_key(key_src),
                    "status": self.normalize_status(fields.get('status', 'unverified')), **self._verdict_fields(auth, fields.get('status', 'unverified')), **typed,
                    "re": fields.get('re', '-')[:200],
                    "statement": head, "evidence": fields.get('evidence', '-')[:2000],
                    "verified_by": fields.get('verifiedby', '-')[:2000], "format": "markdown",
                    **({"parts": len(parts)} if len(parts) > 1 else {})})
            elif not light and 'release' in fields:
                rid = self._append(db, "release", auth["worker"], {"task_key": self._task_key(raw_fields['release']), "reason": head})
            elif not light and post_type not in ('note', 'verdict') and 'claim' in fields and 'task' not in fields and not (
                    (lambda live: live and live['worker'] != auth['worker'])(self._open_claims(db).get(self._task_key(raw_fields['claim'])))):
                rid = self._append(db, "claim", auth["worker"], {"task": fields['claim'][:2000], "task_key": self._task_key(raw_fields['claim']),
                    "plan": head, "help_needed": '-', "format": "markdown"})
            else:
                rid = self._append(db, "comment", auth["worker"], {"text": head, "about": fields.get('re', '-')[:200], "format": "markdown", **typed})
            ids = [rid]
            for i, part in enumerate(parts[1:], 2):
                ids.append(self._append(db, "comment", auth["worker"], {"text": part, "about": "continued from record %d (part %d of %d)" % (rid, i, len(parts)), "format": "markdown", **({"post_type": typed["post_type"]} if "post_type" in typed else {})}))
            if repair_claim is not None:
                return self._append_review_repair(db, auth, repair_claim, repair_target, rid, proposed_repair)
            from .post_types import OPENING
            if route and not post_type and OPENING.match(text):
                # Never refused for format, but the author must learn that its source report is not a claim.
                return {"records": ids, "note": "stored as a note, not a literature claim: no Post-type line in the "
                        "first eight lines. Repost with Post-type: literature as the first line and "
                        "Literature: <card JSON>, or use post_finding with post_type=\"literature\"."}
            return rid if len(ids) == 1 else {"records": ids, "note": "long post stored in %d parts" % len(ids)}

    def post_comment(self, token, arm_id, problem_id, *, text, about=''):
        """Comments, ideas, objections, suggested next steps (all members)."""
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            return self._append(db, "comment", auth["worker"], {"text": text_field(text), "about": text_field(about or '-')})

    def open_claims(self, token, arm_id, problem_id):
        with self._transaction() as db:
            self._auth(db, token, arm_id, problem_id, {"research"})
            return [dict(task=v['task'], worker=v['worker'], record=v['record']) for v in self._open_claims(db).values()]

    def post_note(self, token, arm_id, problem_id, *, kind, scope, evidence, dependencies=()):
        """Unticketed dissent is visible evidence, never an allocation veto."""
        if kind not in {"dissent", "negative-result", "duplicate-nomination"}:
            raise BoardError("invalid note kind")
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            self._visible(auth)
            deps = self._deps(db, dependencies, require_valid=False)
            return self._append(db, kind, auth["worker"],
                                {"scope": text_field(scope), "evidence": text_field(evidence)}, deps=deps)

    def post_help(self, token, arm_id, problem_id, *, obligation, contribution,
                  completion_condition, dependencies, milestone_id=None):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            self._visible(auth)
            obligation = identifier(obligation)
            if not db.execute("SELECT 1 FROM obligations WHERE id=?", (obligation,)).fetchone():
                raise BoardError("help requires an existing assigned obligation")
            deps = self._deps(db, dependencies)
            if milestone_id is None:
                milestone_id = next((ref for ref in deps if self._record(db, ref)["kind"] == "result"), None)
            if milestone_id not in deps or self._record(db, milestone_id)["kind"] != "result":
                raise BoardError("help requires an exact milestone dependency")
            return self._append(db, "help-request", auth["worker"], {
                "obligation": obligation, "contribution": text_field(contribution),
                "completion_condition": text_field(completion_condition),
                "milestone_id": milestone_id}, deps=deps)

    def read_record(self, token, arm_id, problem_id, record_id):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research", "report"})
            self._visible(auth)
            row=self._record(db, record_id)
            self._review_visible(db,auth,row)
            self._planning_visible(db,auth,row)
            return self._public(db, row)

    def wire_records(self, token, arm_id, problem_id, *, record_id):
        """P1 authenticated wire records; a revealed pair yields two reviews."""
        from .board_wire import translate
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research", "report"})
            self._visible(auth)
            row=self._record(db, record_id)
            self._review_visible(db,auth,row)
            self._planning_visible(db,auth,row)
            return translate(self, db, row)

    def export_wire(self, secret):
        """Controller-only committed public records, never private sealed votes."""
        from .board_wire import translate
        self._control(secret)
        with self._transaction() as db:
            return [wire for row in db.execute("SELECT * FROM records ORDER BY id")
                    for wire in translate(self, db, row)]

    def read_artifact(self, token, arm_id, problem_id, artifact_hash):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research", "report"})
            self._visible(auth)
            if not db.execute("SELECT 1 FROM records WHERE artifact=?", (artifact_hash,)).fetchone():
                raise BoardError("unknown artifact")
            for linked in db.execute("SELECT * FROM records WHERE artifact=?", (artifact_hash,)):
                self._review_visible(db,auth,linked)
                self._planning_visible(db,auth,linked)
            row = db.execute("SELECT content FROM artifacts WHERE hash=?", (artifact_hash,)).fetchone()
            if row is None or digest(row[0]) != artifact_hash:
                raise BoardError("artifact integrity failure")
            return row[0]

    def claim(self, secret, *, owner, obligation, method, dependencies=(), cost_microdollars,
              deadline, independent_check=False):
        """Controller calls only after P3 funding/P5 eligibility checks.

        The same obligation keeps its service-generated lineage across methods and
        reassignments. Active bounded requests/tools can only be set by controller.
        """
        self._control(secret)
        obligation, method = identifier(obligation), identifier(method)
        positive_int(cost_microdollars)
        if type(independent_check) is not bool:
            raise BoardError("invalid independent check flag")
        now, deadline = finite(self.clock()), finite(deadline)
        if deadline <= now:
            raise BoardError("expired assignment")
        with self._transaction() as db:
            self._mutable(db, research=True)
            worker = db.execute("SELECT * FROM workers WHERE id=?", (owner,)).fetchone()
            if worker is None or worker["role"] != "research":
                raise BoardError("owner unavailable")
            deps = self._deps(db, dependencies)
            key = digest(canonical([obligation, method, deps]).encode())
            previous = db.execute("SELECT * FROM leases WHERE key=?", (key,)).fetchone()
            if previous and previous["status"] == "active":
                bounded = previous["active_until"] is not None and now < previous["active_until"]
                if now < previous["deadline"] and (bounded or now - previous["updated"] < 1800):
                    raise BoardError("duplicate live lease")
            version = previous["version"] + 1 if previous else 1
            lineage = "lineage-" + secrets.token_hex(16)
            db.execute("INSERT OR IGNORE INTO obligations VALUES (?,?)", (obligation, lineage))
            lineage = db.execute("SELECT lineage FROM obligations WHERE id=?", (obligation,)).fetchone()[0]
            db.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (key, obligation, method, canonical(deps), owner, version, now, deadline,
                        None, cost_microdollars, "active"))
            db.execute("INSERT INTO assignments VALUES (?,?,?)", (key, version, owner))
            record = self._append(db, "assignment", "controller", {
                "key": key, "obligation": obligation, "method": method, "owner": owner,
                "assignment_version": version, "lineage_id": lineage,
                "cost_microdollars": cost_microdollars, "deadline": deadline,
                "independent_check": independent_check}, deps=deps)
            return {"key": key, "assignment_version": version, "lineage_id": lineage,
                    "record_id": record}

    def set_activity(self, secret, key, assignment_version, *, until):
        self._control(secret)
        with self._transaction() as db:
            self._mutable(db, research=True)
            row = db.execute("SELECT * FROM leases WHERE key=?", (key,)).fetchone()
            if row is None or row["version"] != assignment_version or row["status"] != "active":
                raise BoardError("stale assignment")
            if until is not None and not finite(self.clock()) < finite(until) <= row["deadline"]:
                raise BoardError("activity must have an original bounded deadline")
            db.execute("UPDATE leases SET active_until=? WHERE key=?", (until, key))

    def heartbeat(self, token, arm_id, problem_id, *, key, assignment_version):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            row = db.execute("SELECT * FROM leases WHERE key=?", (key,)).fetchone()
            if (row is None or row["owner"] != auth["worker"] or row["version"] != assignment_version
                    or row["status"] != "active" or self.clock() >= row["deadline"]):
                raise BoardError("stale assignment")
            if any(not self._valid(db, ref) for ref in json.loads(row["deps"])):
                raise BoardError("suspended dependency")
            db.execute("UPDATE leases SET updated=? WHERE key=?", (finite(self.clock()), key))

    def post_result(self, token, arm_id, problem_id, *, key, assignment_version,
                    claim, scope, content, limitations, pointers, coauthors=(), claim_type="argument"):
        if claim_type not in {"argument", "computation", "reduction", "obstruction"}:
            raise BoardError("invalid claim type")
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            assigned = db.execute("SELECT owner FROM assignments WHERE key=? AND version=?",
                                  (key, assignment_version)).fetchone()
            if assigned is None or assigned[0] != auth["worker"]:
                raise BoardError("not the authenticated assignment owner")
            lease = db.execute("SELECT * FROM leases WHERE key=?", (key,)).fetchone()
            # Preserve the original dependency set, even after reassignment.
            assignment = db.execute("SELECT body,deps FROM records WHERE kind='assignment'").fetchall()
            original = next(row for row in assignment if json.loads(row[0])["key"] == key
                            and json.loads(row[0])["assignment_version"] == assignment_version)
            deps = json.loads(original["deps"])
            fresh = (lease["version"] == assignment_version and lease["status"] == "active"
                     and self.clock() < lease["deadline"] and all(self._valid(db, x) for x in deps))
            if not isinstance(coauthors, (tuple, list)) or len(coauthors) > 16:
                raise BoardError("invalid coauthors")
            authors = sorted(set([auth["worker"], *coauthors]))
            for worker in authors:
                if not db.execute("SELECT 1 FROM workers WHERE id=?", (worker,)).fetchone():
                    raise BoardError("unknown coauthor")
            hashed = self._artifact(db, content)
            duplicates = [row[0] for row in db.execute("SELECT id FROM records WHERE artifact=?", (hashed,))]
            body = {"claim": text_field(claim), "scope": text_field(scope), "claim_type": claim_type,
                    "limitations": text_field(limitations), "pointers": text_field(pointers),
                    "key": key, "assignment_version": assignment_version,
                    "lineage_id": json.loads(original["body"])["lineage_id"],
                    "authors": authors, "completes_assignment": fresh,
                    "duplicate_content_records": duplicates}
            record = self._append(db, "result", auth["worker"], body, artifact=hashed, deps=deps)
            if fresh:
                db.execute("UPDATE leases SET status='completed',active_until=NULL WHERE key=?", (key,))
            else:
                db.execute("INSERT INTO invalidations VALUES (?,?,?,1,1)",
                           (f"stale:{record}", record, "stale-assignment"))
            return {"record_id": record, "artifact_hash": hashed, "completes_assignment": fresh}

    def publish_final_bundle(self, secret, *, content, claim, scope, authors, dependencies):
        """P6 publishes an exact bundle; this does not grant internal acceptance."""
        self._control(secret)
        with self._transaction() as db:
            self._mutable(db)
            if not authors or len(authors) > 16:
                raise BoardError("final authors required")
            for author in authors:
                if not db.execute("SELECT 1 FROM workers WHERE id=?", (author,)).fetchone():
                    raise BoardError("unknown author")
            deps = self._deps(db, dependencies)
            hashed = self._artifact(db, content)
            record = self._append(db, "final-bundle", "controller", {
                "claim": text_field(claim), "scope": text_field(scope),
                "authors": sorted(set(authors)), "completes_assignment": True},
                artifact=hashed, deps=deps)
            return {"record_id": record, "artifact_hash": hashed}

    def open_review_pair(self, secret, *, candidate_id, reviewers, deadline,
                         exposure, ticket_ids, purpose="allocation"):
        """P5 selects/funds two reviewer identities before calling this method.

        Ticket costs/counts, protected slots, seeded selection, and lineage revision
        eligibility are P5/P3 responsibilities. We independently enforce authorship,
        family, one active pair per worker, candidate identity, and sealed access.
        """
        self._control(secret)
        if purpose not in {"allocation", "completion"}:
            raise BoardError("invalid review purpose")
        if not isinstance(reviewers, (tuple, list)) or len(reviewers) != 2 or len(set(reviewers)) != 2:
            raise BoardError("two distinct reviewers required")
        if (not isinstance(ticket_ids, (tuple, list)) or len(ticket_ids) != 2
                or len(set(ticket_ids)) != 2):
            raise BoardError("two distinct funded ticket identifiers required")
        ticket_ids = [identifier(t) for t in ticket_ids]
        if not isinstance(exposure, dict) or set(exposure) != set(reviewers):
            raise BoardError("record exposure for each reviewer")
        if len(canonical(exposure).encode()) > 8000:
            raise BoardError("exposure metadata too large")
        now, deadline = finite(self.clock()), finite(deadline)
        if not now < deadline <= now + 1800:
            raise BoardError("review exceeds original 30-minute bound")
        with self._transaction() as db:
            self._mutable(db, research=purpose == "allocation")
            candidate = self._public(db, self._record(db, candidate_id))
            required_kind = "final-bundle" if purpose == "completion" else "result"
            if (candidate["kind"] != required_kind or not candidate["valid"]
                    or not candidate["body"].get("completes_assignment")):
                raise BoardError("ineligible candidate")
            if db.execute("SELECT 1 FROM pairs WHERE candidate=? AND state='sealed'", (candidate_id,)).fetchone():
                raise BoardError("candidate already under review")
            authors = set(candidate["body"]["authors"])
            dependency_authors = set()
            for ancestor in self._ancestors(db, candidate_id) - {candidate_id}:
                row = self._record(db, ancestor)
                if row["kind"] in {"review-pair", "dissent", "duplicate-nomination", "allocation",
                                   "invalidation", "challenge-resolution", "report-lock", "freeze"}:
                    raise BoardError("discussion or votes cannot substitute for review evidence")
                dependency_authors.update(json.loads(row["body"]).get("authors", [row["author"]]))
            if len(set(reviewers) & dependency_authors) > 1 or set(reviewers) & authors:
                raise BoardError("review authorship exclusion")
            rows = []
            for reviewer in reviewers:
                worker = db.execute("SELECT * FROM workers WHERE id=?", (reviewer,)).fetchone()
                if worker is None or worker["role"] in {"reviewer", "verifier"}:
                    raise BoardError("reviewer unavailable")
                if db.execute("SELECT 1 FROM pairs WHERE state='sealed' AND (reviewer1=? OR reviewer2=?)",
                              (reviewer, reviewer)).fetchone():
                    raise BoardError("reviewer already assigned")
                rows.append(worker)
            if db.execute("SELECT value FROM meta WHERE key='mixed'").fetchone()[0] == 'True':
                if rows[0]["family"] == rows[1]["family"]:
                    raise BoardError("mixed review requires two families")
            # Exposure and tickets stay private until the whole pair is revealed.
            metadata = {"exposure": exposure, "tickets": dict(zip(reviewers, ticket_ids)),
                        "dependency_authors": sorted(dependency_authors)}
            for prior in db.execute("SELECT exposure FROM pairs"):
                if set(json.loads(prior[0])["tickets"].values()) & set(ticket_ids):
                    raise BoardError("review ticket already used")
            pair = db.execute("INSERT INTO pairs(candidate,candidate_hash,reviewer1,reviewer2,deadline,state,purpose,exposure) VALUES (?,?,?,?,?,'sealed',?,?)",
                              (candidate_id, candidate["artifact"], *reviewers, deadline,
                               purpose, canonical(metadata))).lastrowid
            role = "verifier" if purpose == "completion" else "reviewer"
            tokens = {worker: self._session(db, worker, role, pair) for worker in reviewers}
            return {"pair_id": pair, "candidate_hash": candidate["artifact"], "tokens": tokens}

    def review_packet(self, token, arm_id, problem_id):
        """Only frozen claim/evidence, never peer verdicts, tallies or discussion."""
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"reviewer", "verifier"})
            pair = db.execute("SELECT * FROM pairs WHERE id=?", (auth["pair_id"],)).fetchone()
            if pair is None or pair["state"] != "sealed" or self.clock() >= pair["deadline"]:
                raise BoardError("review is closed")
            packet = []
            ancestors = self._ancestors(db, pair["candidate"])
            if len(ancestors) > 256:
                raise BoardError("review packet oversized; decline without truncating")
            packet_bytes = 0
            for record in sorted(ancestors):
                row = self._record(db, record)
                body = json.loads(row["body"])
                # Supply claim evidence, not provenance labels or persuasive discussion.
                allowed = {key: body[key] for key in ("claim", "scope", "limitations", "pointers",
                           "approach", "assumptions", "target", "claim_type", "evidence",
                           "contribution", "completion_condition", "obligation", "method",
                           "command", "input_hashes", "execution_id", "success") if key in body}
                artifact = None
                if row["artifact"]:
                    artifact = db.execute("SELECT content FROM artifacts WHERE hash=?", (row["artifact"],)).fetchone()[0]
                    if digest(artifact) != row["artifact"]:
                        raise BoardError("artifact integrity failure")
                packet_bytes += len(canonical(allowed).encode()) + len(artifact or b"")
                if packet_bytes > MAX_ARTIFACT_BYTES:
                    raise BoardError("review packet oversized; decline without truncating")
                packet.append({"record_id": record, "kind": row["kind"], "body": allowed,
                               "artifact_hash": row["artifact"], "content": artifact,
                               "dependencies": json.loads(row["deps"])})
            return {"pair_id": pair["id"], "candidate_id": pair["candidate"],
                    "candidate_hash": pair["candidate_hash"], "purpose": pair["purpose"],
                    "deadline": pair["deadline"], "evidence": packet}

    def commit_review(self, token, arm_id, problem_id, *, candidate_hash, verdict,
                      relevant, scope, evidence, check=None):
        if verdict not in {"support", "challenge", "indeterminate"} or type(relevant) is not bool:
            raise BoardError("invalid review verdict")
        if check is not None:
            if (not isinstance(check, dict) or set(check) != {"command", "input_hashes", "scope", "execution_id"}
                    or not all(isinstance(v, str) and v for v in check.values())):
                raise BoardError("check requires pinned inputs, command, exact scope and runner evidence")
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"reviewer", "verifier"})
            self._mutable(db)
            pair = db.execute("SELECT * FROM pairs WHERE id=?", (auth["pair_id"],)).fetchone()
            if (pair is None or pair["state"] != "sealed" or self.clock() >= pair["deadline"]
                    or pair["candidate_hash"] != candidate_hash):
                raise BoardError("review closed or candidate changed")
            if db.execute("SELECT 1 FROM reviews WHERE pair_id=? AND reviewer=?", (pair["id"], auth["worker"])).fetchone():
                raise BoardError("duplicate review submission")
            body = {"verdict": verdict, "relevant": relevant, "scope": text_field(scope),
                    "evidence": text_field(evidence), "check": check,
                    "reviewer": auth["worker"], "family": auth["family"],
                    "candidate_hash": candidate_hash, "committed_at": finite(self.clock())}
            if len(canonical(body).encode()) > MAX_RECORD_BYTES // 4:
                raise BoardError("review too large")
            db.execute("INSERT INTO reviews VALUES (?,?,?,?)",
                       (pair["id"], auth["worker"], canonical(body), finite(self.clock())))
            count = db.execute("SELECT count(*) FROM reviews WHERE pair_id=?", (pair["id"],)).fetchone()[0]
            if count == 2:
                self._reveal(db, pair)
            # No first/second response distinction: a submission receipt reveals no tally.
            return {"committed": True, "pair_id": pair["id"]}

    def _reveal(self, db, pair):
        if pair["state"] != "sealed":
            return
        reviews = [json.loads(row[0]) for row in db.execute(
            "SELECT body FROM reviews WHERE pair_id=? ORDER BY reviewer", (pair["id"],))]
        self._append(db, "review-pair", "controller", {
            "pair_id": pair["id"], "candidate_id": pair["candidate"],
            "candidate_hash": pair["candidate_hash"], "purpose": pair["purpose"],
            "reviews": reviews, "completed": len(reviews) == 2,
            **json.loads(pair["exposure"])}, deps=[pair["candidate"]])
        db.execute("UPDATE pairs SET state='revealed' WHERE id=?", (pair["id"],))
        if any(review["verdict"] == "challenge" for review in reviews):
            self._invalidate(db, root=pair["candidate"], cause=f"review:{pair['id']}",
                             reason="recorded-review-challenge", permanent=False)
        # Fence review sessions on reveal; P2/P5 explicitly restore their next role.
        db.execute("UPDATE workers SET epoch=epoch+1, role='idle' WHERE id IN (?,?)",
                   (pair["reviewer1"], pair["reviewer2"]))

    def expire_reviews(self, secret):
        self._control(secret)
        with self._transaction() as db:
            self._mutable(db)
            pairs = db.execute("SELECT * FROM pairs WHERE state='sealed' AND deadline<=?",
                               (finite(self.clock()),)).fetchall()
            for pair in pairs:
                self._reveal(db, pair)
            return [pair["id"] for pair in pairs]

    def candidate_status(self, secret, candidate_id):
        """A peer assessment is never proof. P5 consumes helper permissions."""
        self._control(secret)
        with self._transaction() as db:
            candidate = self._record(db, candidate_id)
            if not self._valid(db, candidate_id):
                return "suspended"
            pairs = db.execute("SELECT * FROM pairs WHERE candidate=? ORDER BY id DESC", (candidate_id,)).fetchall()
            if not pairs or pairs[0]["state"] != "revealed":
                return "completion-unconfirmed" if candidate["kind"] == "final-bundle" else "provisional"
            pair = pairs[0]
            reviews = [json.loads(r[0]) for r in db.execute("SELECT body FROM reviews WHERE pair_id=?", (pair["id"],))]
            if len(reviews) == 2 and all(r["verdict"] == "support" and r["relevant"] for r in reviews):
                body = json.loads(candidate["body"])
                if body.get("claim_type") == "computation":
                    checks = [json.loads(r[0]) for r in db.execute("SELECT body FROM records WHERE kind='runner-check'")]
                    if not any(check["candidate_id"] == candidate_id and check["success"] for check in checks):
                        return "provisional"
                return "peer-supported-completion" if pair["purpose"] == "completion" else "peer-supported-for-allocation"
            return "completion-unconfirmed" if pair["purpose"] == "completion" else "provisional"

    def candidate_facts(self, secret, candidate_id):
        """Authenticated immutable facts for P5, never worker-supplied authority."""
        self._control(secret)
        with self._transaction() as db:
            candidate = self._public(db, self._record(db, candidate_id))
            if candidate["kind"] not in {"result", "final-bundle"}:
                raise BoardError("not a reviewable candidate")
            dependency_authors = set()
            for ancestor in self._ancestors(db, candidate_id) - {candidate_id}:
                row = self._record(db, ancestor)
                dependency_authors.update(json.loads(row["body"]).get("authors", [row["author"]]))
            body = candidate["body"]
            return {"candidate_id": candidate_id, "artifact_hash": candidate["artifact"],
                    "lineage_id": body.get("lineage_id", "final-bundle"),
                    "author_ids": body["authors"], "dependency_author_ids": sorted(dependency_authors),
                    "dependency_record_ids": candidate["deps"], "valid": candidate["valid"],
                    "completes_assignment": body["completes_assignment"],
                    "claim_type": body.get("claim_type", "argument"),
                    "purpose": "completion" if candidate["kind"] == "final-bundle" else "allocation"}

    def _invalidate(self, db, *, root, cause, reason, permanent):
        self._record(db, root)
        old = db.execute("SELECT * FROM invalidations WHERE cause=?", (cause,)).fetchone()
        if old:
            if old["root"] != root or old["reason"] != reason or bool(old["permanent"]) != permanent:
                raise BoardError("invalidation identifier reused")
            return self._affected(db, root)
        db.execute("INSERT INTO invalidations VALUES (?,?,?,1,?)", (cause, root, reason, int(permanent)))
        affected = self._affected(db, root)
        self._append(db, "invalidation", "controller", {"root": root, "cause": cause,
                     "reason": reason, "permanent": permanent,
                     "affected_count": len(affected), "affected_records": affected[:64]})
        return affected

    def _affected(self, db, root):
        affected = {root}
        # References always point backwards to committed immutable records.
        for row in db.execute("SELECT id,deps FROM records ORDER BY id"):
            if affected.intersection(json.loads(row["deps"])):
                affected.add(row["id"])
        return sorted(affected)

    def record_check(self, secret, *, candidate_id, command, input_hashes, scope,
                     execution_id, success):
        """Record evidence from the trusted bounded deterministic runner, not a worker."""
        self._control(secret)
        if type(success) is not bool:
            raise BoardError("invalid check outcome")
        with self._transaction() as db:
            self._mutable(db)
            candidate = self._record(db, candidate_id)
            return self._append(db, "runner-check", "controller", {
                "candidate_id": candidate_id, "candidate_hash": candidate["artifact"],
                "command": text_field(command), "input_hashes": text_field(input_hashes),
                "scope": text_field(scope), "execution_id": identifier(execution_id),
                "success": success}, deps=[candidate_id])

    def record_allocation(self, secret, *, rule, evidence_ids, old_owner, new_owner,
                          cost_microdollars, deadline, assignment_version=1, helper_permission_id=None):
        self._control(secret)
        positive_int(cost_microdollars)
        positive_int(assignment_version)
        if helper_permission_id is not None:
            helper_permission_id = identifier(helper_permission_id)
        if new_owner is None:
            raise BoardError("new owner required")
        with self._transaction() as db:
            self._mutable(db, research=True)
            deps = self._deps(db, evidence_ids)
            for worker in (old_owner, new_owner):
                if worker is not None and not db.execute("SELECT 1 FROM workers WHERE id=?", (worker,)).fetchone():
                    raise BoardError("unknown allocation owner")
            if finite(deadline) <= self.clock():
                raise BoardError("expired allocation")
            return self._append(db, "allocation", "controller", {
                "rule": identifier(rule), "evidence_ids": deps, "old_owner": old_owner,
                "new_owner": new_owner, "cost_microdollars": cost_microdollars,
                "deadline": deadline, "assignment_version": assignment_version,
                "helper_permission_id": helper_permission_id}, deps=deps)

    def invalidate(self, secret, *, record_id, cause, evidence, permanent=False):
        """Only admitted funded challenges or deterministic counterevidence call here.

        Worker free-form criticism cannot reach this controller endpoint. Each cause
        is idempotent, so replay cannot renew a resolved challenge suspension.
        """
        self._control(secret)
        if type(permanent) is not bool:
            raise BoardError("invalid permanence")
        with self._transaction() as db:
            self._mutable(db)
            return self._invalidate(db, root=record_id, cause=identifier(cause),
                                    reason=text_field(evidence), permanent=permanent)

    def resolve_challenge(self, secret, *, cause, evidence):
        self._control(secret)
        with self._transaction() as db:
            self._mutable(db)
            row = db.execute("SELECT * FROM invalidations WHERE cause=?", (cause,)).fetchone()
            if row is None or row["permanent"]:
                raise BoardError("no reversible challenge")
            if row["active"]:
                db.execute("UPDATE invalidations SET active=0 WHERE cause=?", (cause,))
                self._append(db, "challenge-resolution", "controller", {
                    "cause": cause, "evidence": text_field(evidence)})

    def withdraw(self, token, arm_id, problem_id, *, record_id, evidence):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research"})
            self._mutable(db, research=True)
            record = self._record(db, record_id)
            if record["author"] != auth["worker"]:
                raise BoardError("only author may withdraw")
            return self._invalidate(db, root=record_id, cause=f"withdraw:{record_id}",
                                    reason=text_field(evidence), permanent=True)

    def affected_leases(self, secret):
        self._control(secret)
        with self._transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM leases WHERE status='active'")
                    if any(not self._valid(db, dep) for dep in json.loads(row["deps"]))]

    def changes(self, token, arm_id, problem_id, *, after=0, max_tokens=1000):
        """Deterministic pointers, bounded by a conservative UTF-8 byte token bound.

        The runtime schedules periodic reads at worker request boundaries.
        Evidence is charged by P3 when included in a model request. No generated summary.
        """
        try:
            after = max(0, int(after or 0)); max_tokens = min(1000, max(128, int(max_tokens or 1000)))
        except (TypeError, ValueError):
            after, max_tokens = 0, 1000
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {"research", "report"})
            self._visible(auth)
            self._mutable(db)  # No operational writes into a frozen scientific DB.
            now = finite(self.clock())

            result = {"after": after, "cursor": after, "changes": [], "more": False}
            for row in db.execute("SELECT * FROM records WHERE id>? ORDER BY id LIMIT 100", (after,)):
                if row["kind"] in ("planner-input","planner-read","global-plan"):
                    result["cursor"]=row["id"]; continue
                try:self._review_visible(db,auth,row)
                except BoardError:
                    result['cursor']=row['id'];continue
                item = {"id": row["id"], "kind": row["kind"], "valid": self._valid(db, row["id"])}
                trial = {**result, "cursor": row["id"], "changes": result["changes"] + [item], "more": True}
                if len(canonical(trial).encode()) > max_tokens:
                    result["more"] = True
                    break
                result = trial
            else:
                result["more"] = bool(db.execute("SELECT 1 FROM records WHERE id>?", (result["cursor"],)).fetchone())
            encoded = canonical(result)
            db.execute("INSERT INTO digest_reads VALUES (?,?)", (auth["worker"], now))
            return {"text": encoded, "token_upper_bound": len(encoded.encode()),
                    "cursor": result["cursor"], "more": result["more"]}

    def enter_report_mode(self, secret):
        self._control(secret)
        with self._transaction() as db:
            self._enter_report_mode(db)

    def _enter_report_mode(self, db, *, pressure=None):
        self._mutable(db)
        if self._mode(db) == "report":
            return
        # Research review pairs cannot continue across the report lock.
        for pair in db.execute("SELECT * FROM pairs WHERE state='sealed'").fetchall():
            self._reveal(db, pair)
        db.execute("UPDATE meta SET value='report' WHERE key='mode'")
        db.execute("UPDATE workers SET epoch=epoch+1,role='idle'")
        body = {"one_way": True}
        if pressure is not None:
            body["history_pressure"] = pressure
        self._append(db, "report-lock", "controller", body)

    def fence_history_pressure(self, secret, limit):
        """Fence before the single RPC service accepts another worker mutation.

        The just-committed record is retained. Controller publications remain
        available for admitted replies and compulsory closing. The report-lock
        record persists the reason independently of controller polling.
        """
        self._control(secret)
        if type(limit) is not int or limit <= 0:
            raise BoardError("invalid infrastructure history limit")
        with self._transaction() as db:
            if self._mode(db) != "research":
                return None
            size = len(self._snapshot(db).encode())
            if size < limit:
                return None
            pressure = {"reason": "infrastructure-history-pressure", "bytes": size,
                        "threshold_bytes": limit}
            self._enter_report_mode(db, pressure=pressure)
            return pressure

    def publish_control_record(self, secret, *, identity, kind, body):
        """Immutable decision/receipt/notes; no worker can invoke this method."""
        self._control(secret)
        if kind not in {'decision','action-receipts','orchestrator-notes','final-findings','worker-result','planner-input','approaches','approach-critique'}:
            raise BoardError('invalid control record kind')
        with self._transaction() as db:
            self._mutable(db)
            content=canonical(body).encode()
            hashed=digest(content)
            for row in db.execute('SELECT id,body FROM records WHERE kind=?',(kind,)):
                old=json.loads(row['body'])
                if old.get('identity')==identity:
                    if old.get('content_sha256')!=hashed:raise BoardError('immutable control record')
                    return row['id']
            if kind=='planner-input':
                manifest=dict(body)
                pages=manifest.pop('pages')
                manifest['page_artifacts']=[self._artifact(db,page.encode()) for page in pages]
                content=canonical(manifest).encode()
            artifact=self._artifact(db,content)
            return self._append(db,kind,'controller',dict(identity=identity,content_sha256=hashed),artifact=artifact)

    def planning_snapshot(self, secret, *, question, assignments, policy):
        """Complete ledger and cutoff from one board transaction, not racing reads."""
        self._control(secret)
        from .global_planner import make_input
        with self._transaction() as db:
            records=[self._public(db,r) for r in db.execute('SELECT * FROM records ORDER BY id')]
            claims=self._ledger_of(db,records,policy)
            data=make_input(question,claims,assignments,max((r['id'] for r in records),default=0),policy)
            data['bound_reviews']=policy.get('bound_reviews',{})
            return data

    def _planning_visible(self, db, auth, row):
        if row['kind'] in ('planner-input','planner-read','global-plan'):
            raise BoardError('planning records are private to planner/coordinator; use authenticated planner_page')

    def _planning_input(self, db, auth, input_record, hydrate=True):
        row = db.execute("SELECT * FROM records WHERE id=? AND kind='planner-input'", (input_record,)).fetchone()
        if row is None: raise BoardError('unknown planning input')
        data = json.loads(db.execute('SELECT content FROM artifacts WHERE hash=?', (row['artifact'],)).fetchone()[0])
        if auth['worker'] != data['owner'] or auth['model'] != 'K3':
            raise BoardError('unauthorized planner input')
        if data.get('page_artifacts') and hydrate:
            data['pages']=[]
            for page_hash in data['page_artifacts']:
                part=db.execute('SELECT content FROM artifacts WHERE hash=?',(page_hash,)).fetchone()
                if part is None or digest(part[0])!=page_hash:raise BoardError('missing planner page or integrity failure')
                data['pages'].append(bytes(part[0]).decode())
        return data

    def planner_page(self, token, arm_id, problem_id, *, input_record, page=0):
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {'research'})
            data = self._planning_input(db, auth, input_record, hydrate=False)
            count=len(data.get('page_artifacts') or data.get('pages',[]))
            if type(page) is not int or not 0 <= page < count: raise BoardError('missing planner page')
            if data.get('page_artifacts'):
                part=db.execute('SELECT content FROM artifacts WHERE hash=?',(data['page_artifacts'][page],)).fetchone()
                if part is None or digest(part[0])!=data['page_hashes'][page]:raise BoardError('missing planner page or integrity failure')
                content=bytes(part[0]).decode()
            else:content=data['pages'][page]
            reads = [json.loads(r[0]) for r in db.execute("SELECT body FROM records WHERE author=? AND kind='planner-read'", (auth['worker'],))]
            if not any(r.get('input_record') == input_record and r.get('page') == page for r in reads):
                self._append(db, 'planner-read', auth['worker'], dict(input_record=input_record, page=page, sha256=data['page_hashes'][page]))
            return dict(input_id=data['input_id'], board_cutoff=data['board_cutoff'], page=page,
                        page_hashes=data['page_hashes'], content=content)

    def _planning_input_for_refresh(self, db, rid):
        row = db.execute("SELECT artifact FROM records WHERE id=?", (rid,)).fetchone()
        return json.loads(db.execute('SELECT content FROM artifacts WHERE hash=?', (row['artifact'],)).fetchone()[0])

    @staticmethod
    def _planning_refresh_reply(rid, data):
        return dict(status='stale-input', input_record=rid, input_id=data['input_id'],
                    board_cutoff=data['board_cutoff'], pages=len(data.get('page_artifacts') or data['pages']),
                    refresh_count=data['refresh_count'],
                    instruction='Read every refreshed page, reconsider the plan, and retry post_plan with this input. No plan was published.')

    def post_plan(self, token, arm_id, problem_id, *, input_record, plan):
        from .global_planner import validate_plan, fresh, hashed
        with self._transaction() as db:
            auth = self._auth(db, token, arm_id, problem_id, {'research'})
            self._mutable(db, research=True)
            data = self._planning_input(db, auth, input_record)
            reads = {json.loads(r[0])['page'] for r in db.execute("SELECT body FROM records WHERE author=? AND kind='planner-read'", (auth['worker'],)) if json.loads(r[0]).get('input_record') == input_record}
            if reads != set(range(len(data['pages']))): raise BoardError('incomplete planner view: read every page')
            try: validate_plan(plan, data, input_record)
            except ValueError as exc: raise BoardError(str(exc)) from exc
            records = [self._public(db,r) for r in db.execute('SELECT * FROM records ORDER BY id')]
            from .claim_ledger import load_policy
            assignments = {}
            if self.planning_state_path is not None:
                # Read-only WAL snapshot: never acquire the executor writer lock while
                # holding the board transaction (staffing takes these in the reverse order).
                with sqlite3.connect('file:'+str(self.planning_state_path)+'?mode=ro', uri=True) as executor_db:
                    assignments = json.loads(executor_db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])['assignments']
            policy=dict(load_policy(self.arm_id), bound_reviews={
                k:r['bound_record'] for k,r in assignments.items() if r.get('bound_record') is not None})
            claims = self._ledger_of(db, records,policy)
            if not fresh(data, claims, plan):
                # A rejection must provide a usable next input, atomically and durably.
                # A replay of the same old submission returns the same refresh rather
                # than consuming the budget or creating orphan planning inputs.
                for row in db.execute("SELECT id FROM records WHERE kind='planner-input' AND author='controller' ORDER BY id"):
                    candidate = self._planning_input_for_refresh(db, row['id'])
                    if candidate.get('owner') == auth['worker'] and candidate.get('refresh_of') == input_record:
                        return self._planning_refresh_reply(row['id'], candidate)
                count = data.get('refresh_count', 0)
                if count >= 3:
                    raise BoardError('stale planning input: refresh limit reached; end session so the harness retries the round')
                from .global_planner import make_input
                payload = json.loads(''.join(data['pages']))
                refreshed = make_input(payload['question'], claims, assignments,
                                       max((r['id'] for r in records), default=0), policy)
                refreshed.update(owner=auth['worker'], bound_reviews=policy['bound_reviews'],
                                 refresh_of=input_record, refresh_count=count+1)
                pages = refreshed.pop('pages')
                refreshed['page_artifacts'] = [self._artifact(db, page.encode()) for page in pages]
                artifact = self._artifact(db, canonical(refreshed).encode())
                rid = self._append(db, 'planner-input', 'controller',
                                   dict(identity=auth['worker']+':refresh:'+str(input_record)), artifact=artifact)
                return self._planning_refresh_reply(rid, refreshed)
            body = dict(plan_id=hashed([auth['worker'],plan]), input_record=input_record,
                        board_cutoff=data['board_cutoff'], dependencies=[d for d in data['dependencies'] if d['record'] in set(plan['established']+plan['conditional']+plan['blockers']+plan['select'])], plan=plan)
            dependencies=body.pop('dependencies')
            manifest=self._artifact(db,canonical(dependencies).encode())
            body['dependency_manifest']=manifest
            for r in db.execute("SELECT id,body FROM records WHERE kind='global-plan' AND author=?", (auth['worker'],)):
                if json.loads(r['body']) == body: return r['id']
                raise BoardError('immutable plan: use next planning round')
            return self._append(db,'global-plan',auth['worker'],body,artifact=manifest)

    def plans(self, secret):
        """Controller-only handoffs with the complete private dependency manifest."""
        self._control(secret)
        with self._transaction() as db:
            out=[]
            for row in db.execute("SELECT body,artifact FROM records WHERE kind='global-plan' ORDER BY id"):
                body=json.loads(row['body'])
                if body.get('dependency_manifest'):
                    part=db.execute('SELECT content FROM artifacts WHERE hash=?',(body['dependency_manifest'],)).fetchone()
                    if part is None or digest(part[0])!=body['dependency_manifest']:raise BoardError('plan dependency integrity failure')
                    body['dependencies']=json.loads(part[0])
                out.append(body)
            return out

    def head(self, secret):
        """Controller-only: (highest record id, record count) -- a cheap board-change key."""
        self._control(secret)
        with self._transaction() as db:
            row = db.execute("SELECT COALESCE(MAX(id),0), COUNT(*) FROM records").fetchone()
            return (row[0], row[1])

    def export_snapshot(self, secret):
        """Controller-only immutable copy for acknowledged context handover.

        Includes public records and exact artifact bytes, never session tokens
        or sealed votes. Does not change the board's admission mode.
        """
        self._control(secret)
        with self._transaction() as db:
            snapshot = self._snapshot(db)
            return {"sha256": digest(snapshot.encode()), "snapshot": snapshot}

    def freeze(self, secret):
        """Fence writers and produce an immutable scientific export for P6/P7.

        Not itself the cohort judging embargo or a durability acknowledgement.
        P6 must freeze every registered arm before exposing any export to judging.
        """
        self._control(secret)
        with self._transaction() as db:
            if self._mode(db) != "frozen":
                for pair in db.execute("SELECT * FROM pairs WHERE state='sealed'").fetchall():
                    self._reveal(db, pair)
                db.execute("UPDATE workers SET epoch=epoch+1,role='idle'")
                self._append(db, "freeze", "controller", {"arm_id": self.arm_id, "problem_id": self.problem_id})
                db.execute("UPDATE meta SET value='frozen' WHERE key='mode'")
                snapshot = self._snapshot(db)
                db.execute("INSERT INTO meta VALUES ('frozen_snapshot',?)", (snapshot,))
            else:
                snapshot = db.execute("SELECT value FROM meta WHERE key='frozen_snapshot'").fetchone()[0]
            return {"sha256": digest(snapshot.encode()), "snapshot": snapshot}

    def _snapshot(self, db):
        import base64
        from .board_wire import translate
        for row in db.execute("SELECT * FROM artifacts"):
            if digest(row["content"]) != row["hash"]:
                raise BoardError("artifact integrity failure")
        return canonical({"schema_version": SCHEMA_VERSION, "arm_id": self.arm_id,
                          "problem_id": self.problem_id, "cohort_id": self.cohort_id,
                          "contract_version": CONTRACT_VERSION, "contracts_sha256": self.contracts_hash,
                          "wire_records": [wire for row in db.execute("SELECT * FROM records ORDER BY id")
                                           for wire in translate(self, db, row)],
                          "records": [self._public(db, row) for row in db.execute("SELECT * FROM records ORDER BY id")],
                          "artifacts": [{"sha256": row["hash"], "base64": base64.b64encode(row["content"]).decode()}
                                        for row in db.execute("SELECT * FROM artifacts ORDER BY hash")]})


class BoardClient:
    """Narrow worker surface; transport should allow only these named operations.

    This Python facade is not a sandbox. Do not hand a Board instance or Python
    interpreter in the service process to an untrusted worker.
    """
    OPERATIONS = frozenset({"post_initial", "post_note", "post_help", "read_record", "wire_records", "read_artifact",
                            "heartbeat", "post_result", "review_packet", "commit_review",
                            "withdraw", "changes", "post_claim", "post_finding", "release_claim", "post_comment", "open_claims", "post", "planner_page", "post_plan", "review_page"})
    # Writes that fall back to a plain markdown note instead of being refused.
    NOTE_FALLBACK = frozenset({"post_initial", "post_note", "post_help", "post_result", "withdraw", "post_claim",
                               "post_finding", "release_claim", "post_comment"})
    HARD_ERRORS = ("repair", "REJECT", "bound revision", "Premise-refs","incomplete evidence coverage", "peer decisions withheld", "literature", "source version", "Post-type", "Revision-of", "verdict requires", "unauthorized", "board writes fenced", "empty text", "text is ", "frozen", "unknown worker")

    def __init__(self, board, token, arm_id, problem_id):
        self._board, self._token = board, token
        self.arm_id, self.problem_id = arm_id, problem_id

    # Silent-spend diagnosis 2026-10-06: workers' board writes failed on argument
    # names (id vs record_id, body vs text, extra keys). Accept common aliases and
    # drop unknown keys with a warning instead of refusing the whole call.
    ALIASES = {"id": "record_id", "record": "record_id", "pointer": "record_id", "body": "text",
               "comment": "text", "note": "text", "message": "text", "content_text": "text",
               "summary": "statement", "result": "statement", "claim": "statement",
               "lease_key": "key", "assignment_key": "key", "version": "assignment_version",
               "task_id": "task", "description": "task", "hash": "artifact_hash"}

    @classmethod
    def schema(cls, board_cls=None):
        import inspect
        board_cls = board_cls or Board
        out = {}
        for op in sorted(cls.OPERATIONS):
            fn = getattr(board_cls, op, None)
            if fn is None:continue
            params = list(inspect.signature(fn).parameters.values())[4:]
            out[op] = {p.name: ('optional' if p.default is not inspect.Parameter.empty else 'required') for p in params}
        return out

    OP_ALIASES = {"note": "post", "comment": "post", "post_markdown": "post", "write": "post", "add_note": "post",
                  "post_note_markdown": "post", "finding": "post_finding", "claim": "post_claim", "read": "read_record",
                  "get_record": "read_record", "list_claims": "open_claims", "release": "release_claim"}

    @staticmethod
    def _as_markdown(operation, kwargs):
        lines = []
        for key, value in kwargs.items():
            if not isinstance(value, str):
                value = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, bytes) else value.decode(errors='replace')
            lines.append("%s: %s" % (key.replace('_', ' ').capitalize(), value))
        return ("(" + operation + ")\n" + "\n".join(lines)).strip()

    def call(self, operation, **kwargs):
        try:
            return self._call(operation, **kwargs)
        except BoardError as exc:
            op = self.OP_ALIASES.get(str(operation).strip().lower(), str(operation).strip().lower())
            if op not in self.NOTE_FALLBACK or any(h in str(exc) for h in self.HARD_ERRORS):
                raise
            # Operator ruling 2026-10-06: never deny a write for format; keep it as a note.
            text = self._as_markdown(op, {k: v for k, v in kwargs.items() if k not in ("token", "arm_id", "problem_id")})
            if not text.strip() or text.strip() == "(" + op + ")":
                raise BoardError("%s: %s. Nothing to post. %s" % (op, exc, POST_EXAMPLE))
            # Review 8e7c704 #3: never truncate; split into several posts when needed.
            pieces, step = [], self._board.POST_LIMIT // 6
            while text:
                pieces.append(text[:step]); text = text[step:]
            ids = [self._board._markdown(self._token, self.arm_id, self.problem_id, p, route=False) for p in pieces]
            result = ids[0] if len(ids) == 1 else {"records": ids, "note": "stored in %d posts" % len(ids)}
            return {"result": result, "warning": "%s was not accepted as structured (%s); it was posted as a markdown note. "
                    "Simplest form: board_call post {\"text\": \"...\"}" % (op, exc)}

    def _call(self, operation, **kwargs):
        import inspect
        operation = self.OP_ALIASES.get(str(operation).strip().lower(), str(operation).strip().lower())
        if operation not in self.OPERATIONS:
            raise BoardError("unknown worker operation %r: use post {text} for anything, or one of %s. %s" % (
                operation, ", ".join(sorted(self.OPERATIONS)), POST_EXAMPLE))
        kwargs = {k: v for k, v in kwargs.items() if k not in ("token", "arm_id", "problem_id")}  # transport-bound
        kwargs = {str(k).strip().lower().replace('-', '_').replace(' ', '_'): v for k, v in kwargs.items()}
        fn = getattr(self._board, operation)
        params = list(inspect.signature(fn).parameters.values())[3:]
        names = {p.name for p in params}
        args, dropped = {}, []
        for key, value in kwargs.items():
            name = key if key in names else self.ALIASES.get(key)
            if name in names and name not in args:
                args[name] = value
            else:
                dropped.append(key)
        missing = [p.name for p in params if p.default is inspect.Parameter.empty and p.name not in args]
        if missing:
            raise BoardError("%s needs arguments %s; required %s; got %s" % (operation, ", ".join(missing),
                ", ".join(p.name for p in params if p.default is inspect.Parameter.empty), ", ".join(sorted(kwargs)) or "none"))
        result = fn(self._token, self.arm_id, self.problem_id, **args)
        if dropped:
            return {"result": result, "warning": "ignored unknown arguments: " + ", ".join(dropped)}
        return result
