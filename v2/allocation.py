"""Deterministic swarm allocation. Controller-only; never expose to workers.

Node-local SQLite serializes admission and survives restart. These commitments
bound scheduling, not invoices: P3 must still reserve every request before send.
P4 authenticates candidate facts, material-progress attestations and review seals.
No provider, agent loop, credentials or model inference is implemented here.
"""
from contextlib import contextmanager
from hashlib import sha256
from itertools import combinations
import json
import os
from pathlib import Path
import sqlite3
import time

from .contracts_v3 import canonical_json, contracts_sha256, load_contracts


class AllocationDenied(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise AllocationDenied(reason)


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum, 'integer-required')
    return value


def identifier(value):
    require(isinstance(value, str) and 0 < len(value) <= 256, 'invalid-id')
    return value


def digest(value):
    require(isinstance(value, str) and len(value) == 64 and
            all(c in '0123456789abcdef' for c in value), 'invalid-hash')
    return value


class Allocation:
    """One pinned arm/problem. Timestamps are absolute integer Unix milliseconds.

    ``workers`` maps controller-issued identities to P1 model keys. ``funded`` and
    safe-boundary state come from the controller/P3, never board-authored text.
    The optional clock is for fake-provider tests; defaults to wall time in ms.
    """

    def __init__(self, path, *, arm_id, problem_id, strategy, profile, seed,
                 started_at_ms, workers, deadline_ms=None, clock=None,
                 contracts_dir=None, expected_contracts_sha256=None):
        self.path = Path(path)
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.contracts = load_contracts(contracts_dir, expected_sha256=expected_contracts_sha256)
        require(strategy in ('dsf-swarm', 'mixed-swarm'), 'swarm-only')
        require(profile in ('production', 'pilot', 'swarm-check', 'solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue'), 'unknown-profile')
        self.policy = self.contracts[profile][strategy]
        if self.policy.get('orchestration') == 'stateless-v1':
            raise AllocationDenied('retired-fixed-allocation; use EventExecutor with explicit migration')
        self.rules = self.contracts['rules']
        integer(started_at_ms)
        deadline_ms = deadline_ms if deadline_ms is not None else started_at_ms + self.policy['wall_seconds'] * 1000
        require(started_at_ms < integer(deadline_ms) <= started_at_ms + self.policy['wall_seconds'] * 1000, 'invalid-deadline')
        require(isinstance(seed, str) and seed, 'seed-required')
        require(isinstance(workers, dict) and workers, 'roster-required')
        for worker, model in workers.items():
            identifier(worker)
            require(model in self.policy['models'] and model != 'glm', 'invalid-core-model')
        for model in set(workers.values()):
            require(list(workers.values()).count(model) <= self.policy['slots'][model], 'slot-limit')
        self.config = dict(arm_id=identifier(arm_id), problem_id=identifier(problem_id),
                           strategy=strategy, profile=profile, seed=seed,
                           started_at_ms=started_at_ms, deadline_ms=deadline_ms,
                           workers=workers, contracts_sha256=contracts_sha256(contracts_dir), version=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        require(not self.path.is_symlink(), 'database-symlink')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS allocation (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, state TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT config FROM allocation WHERE id=1').fetchone()
            encoded = canonical_json(self.config).decode()
            if row:
                require(row[0] == encoded, 'immutable-config-mismatch')
            else:
                state = dict(phase='breadth' if strategy == 'dsf-swarm' else 'research',
                             continuation_started_ms=None, closing_deadline_ms=None,
                             funded=sorted(workers), busy=[], candidates={}, pairs={}, tickets={},
                             helpers={}, assignments={}, challenges={}, lineages={}, obligations={},
                             spent={}, phase_liability={}, parent_liability=0, helper_spend={}, review_cursor=0, events=[], seq=0,
                             glm_failures=0, glm_active=None, invalid={}, last_now_ms=started_at_ms)
                db.execute('INSERT INTO allocation VALUES (1,?,?)', (encoded, json.dumps(state)))
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        operation_started = False
        try:
            db.execute('BEGIN IMMEDIATE')
            s = json.loads(db.execute('SELECT state FROM allocation WHERE id=1').fetchone()[0])
            now = max(integer(self.clock()), s['last_now_ms'])
            s['last_now_ms'] = now
            self._advance(s, now)
            # Persist clock/expiry even when the requested operation is refused.
            db.execute('UPDATE allocation SET state=? WHERE id=1', (json.dumps(s, sort_keys=True),))
            db.execute('SAVEPOINT operation')
            operation_started = True
            yield s, now
            db.execute('UPDATE allocation SET state=? WHERE id=1', (json.dumps(s, sort_keys=True),))
            db.commit()
        except AllocationDenied:
            if operation_started:
                db.execute('ROLLBACK TO operation')
                db.commit()
            else:
                db.rollback()
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, s, rule, **facts):
        s['seq'] += 1
        event = dict(sequence=s['seq'], rule=rule, at_ms=s['last_now_ms'], **facts)
        s['events'].append(event)
        return event

    def _id(self, s, kind):
        return f"{self.config['arm_id']}:{kind}:{s['seq'] + 1}"

    def _rank(self, name):
        return sha256(canonical_json([self.config['seed'], name])).hexdigest(), name

    def _pool(self, model, kind, s):
        if self.config['strategy'] == 'mixed-swarm':
            return model + ':' + kind
        return kind if kind != 'research' else s['phase']

    def _limit(self, pool):
        if ':' in pool:
            model, kind = pool.split(':')
            return self.policy['model_pools_microusd'][model][kind]
        return self.policy['pools_microusd'][pool]

    def _available(self, s, pool):
        return self._limit(pool) - s['spent'].get(pool, 0)

    def _commit(self, s, pool, amount):
        integer(amount, 1)
        require(self._available(s, pool) >= amount, 'pool-exhausted')
        s['spent'][pool] = s['spent'].get(pool, 0) + amount

    def _close(self, s, now, reason):
        if s['phase'] in ('delivery', 'terminal'):
            return
        s['phase'] = 'delivery'
        s['closing_deadline_ms'] = min(self.config['deadline_ms'], now + self.policy.get('closing_max_seconds', self.policy['wall_seconds']) * 1000)
        for t in s['tickets'].values():
            if t['state'] == 'active' and t['kind'] != 'completion':
                t['state'] = 'cancel-required'
        for a in list(s['assignments'].values()) + list(s['helpers'].values()):
            if a['state'] == 'active':
                a['state'] = 'cancel-required'
        self._event(s, 'enter-delivery', reason=reason, deadline_ms=s['closing_deadline_ms'])

    def _advance(self, s, now):
        if s['phase'] == 'terminal':
            return
        if now >= self.config['deadline_ms'] or (s['closing_deadline_ms'] is not None and now >= s['closing_deadline_ms']):
            self._close(s, now, 'original-deadline')
            s['phase'] = 'terminal'
            self._event(s, 'terminal', reason='original-deadline')
        start = self.config['started_at_ms']
        if s['phase'] == 'breadth' and (now >= start + self.policy['breadth_stop_seconds'] * 1000 or s.get('phase_liability', {}).get('breadth', 0) >= self._limit('breadth')):
            s['phase'] = 'continuation'
            s['continuation_started_ms'] = min(now, start + self.policy['breadth_stop_seconds'] * 1000)
            self._event(s, 'breadth-to-continuation', maximum_workers=4)
            retained = set(self._staffing(s)['active'])
            for item in list(s['tickets'].values()) + list(s['assignments'].values()):
                if item.get('worker') not in retained and item['state'] == 'active':
                    item['state'] = 'cancel-required'
        if s['phase'] == 'continuation' and (now >= min(start + self.policy['continuation_absolute_stop_seconds'] * 1000,
                s['continuation_started_ms'] + self.policy['continuation_max_seconds'] * 1000) or s.get('phase_liability', {}).get('continuation', 0) >= self._limit('continuation')):
            self._close(s, now, 'continuation-bound')
        if s['phase'] not in ('delivery', 'terminal'):
            research_stop = self.policy.get('research_stop_seconds')
            if research_stop is not None and now >= start + research_stop * 1000:
                self._close(s, now, 'research-clock')
            elif now >= start + (self.config['deadline_ms'] - start) * 90 // 100:
                self._close(s, now, 'wall-writing-trigger')
            elif s.get('parent_liability', 0) * 100 >= self.policy['cap_microusd'] * 90:
                self._close(s, now, 'budget-writing-trigger')
            elif len(s['funded']) < 2:
                self._close(s, now, 'insufficient-funded-core')
        for ticket in s['tickets'].values():
            if ticket['state'] == 'active' and now >= ticket['deadline_ms']:
                ticket['state'] = 'expired'
                self._event(s, 'ticket-expired', ticket_id=ticket['id'])
                if ticket['kind'] == 'glm':
                    s['glm_active'] = None
        for a in list(s['helpers'].values()) + list(s['assignments'].values()):
            if a['state'] in ('active', 'suspended') and now >= a['deadline_ms']:
                a['state'] = 'expired'
                self._event(s, 'assignment-expired', assignment_id=a['id'])
        for pair in s['pairs'].values():
            if pair['state'] == 'pending' and now >= pair['deadline_ms']:
                pair['state'] = 'expired'
                self._event(s, 'pair-expired', pair_id=pair['id'])

    def _staffing(self, s):
        maximum = 8 if s['phase'] == 'breadth' else 4 if self.config['strategy'] == 'dsf-swarm' else 5
        ranked = sorted(s['funded'], key=self._rank)
        active = ranked[:maximum]
        explorers = active[:2 if len(active) > 4 and self.config['strategy'] == 'dsf-swarm' else 1]
        challengers = active[len(explorers):len(explorers) + 1] if len(active) >= 3 else []
        return dict(active=active, explorers=explorers, challengers=challengers,
                    retiring=ranked[maximum:], maximum=maximum, phase=s['phase'])

    def snapshot(self):
        with self._transaction() as (s, now):
            return json.loads(json.dumps(dict(s, staffing=self._staffing(s))))

    def set_staffing(self, funded, *, busy=()):
        """Authenticated funding/slot observations. Busy includes draining old work."""
        with self._transaction() as (s, now):
            require(set(funded) <= set(self.config['workers']) and len(set(funded)) == len(funded), 'unknown-worker')
            require(set(busy) <= set(self.config['workers']), 'unknown-busy-worker')
            s['funded'], s['busy'] = sorted(funded), sorted(set(busy))
            protected = set(self._staffing(s)['explorers'])
            require(not any(t.get('worker') in protected and not t.get('slot_released', False)
                            for t in s['tickets'].values()), 'protected-slot-awaiting-fence')
            self._advance(s, now)
            self._event(s, 'staffing', **self._staffing(s))
            return self._staffing(s)

    def observe_liability(self, *, pools, parent_microusd):
        """P3's settled + unresolved + active reservations, NOT carved account
        ceilings. Earmarking all eight breadth envelopes must still allow all
        eight workers to start; actual admitted liability triggers a phase stop.
        """
        with self._transaction() as (s, now):
            for pool, value in pools.items():
                integer(value)
                try:
                    self._limit(pool)
                except (KeyError, ValueError):
                    raise AllocationDenied('unknown-pool') from None
            s['phase_liability'] = dict(pools)
            s['parent_liability'] = integer(parent_microusd)
            self._advance(s, now)
            self._event(s, 'liability-observation', pools=pools, parent_microusd=parent_microusd)

    def enter_delivery(self, reason):
        with self._transaction() as (s, now):
            self._close(s, now, identifier(reason))

    def _research(self, s):
        require(s['phase'] not in ('delivery', 'terminal'), 'research-closed')

    def register_obligation(self, obligation_id, *, ancestry=(), purpose='allocation'):
        """IDs/ancestry come from P4's controller-issued lease, never an author name."""
        with self._transaction() as (s, now):
            require(purpose in ('allocation', 'completion'), 'invalid-purpose')
            require(s['phase'] != 'terminal', 'arm-terminal')
            if purpose == 'allocation':
                self._research(s)
            identifier(obligation_id)
            require(set(ancestry) <= set(s['candidates']), 'unknown-ancestry')
            if obligation_id in s['obligations']:
                require(s['obligations'][obligation_id]['ancestry'] == sorted(set(ancestry)) and s['obligations'][obligation_id].get('purpose', 'allocation') == purpose, 'obligation-ancestry-mismatch')
            else:
                lineage = self._id(s, 'lineage')
                s['obligations'][obligation_id] = dict(lineage=lineage, ancestry=sorted(set(ancestry)), purpose=purpose)
                s['lineages'][lineage] = dict(pairs=0, challenges=0)
                self._event(s, 'register-obligation', obligation_id=obligation_id, lineage=lineage)
            return s['obligations'][obligation_id]['lineage']

    def register_candidate(self, candidate_id, *, obligation_id, artifact_hash,
                           authors, dependencies=(), progress_evidence=None, purpose='allocation',
                           full_scope=False, check_receipt=None, claim_type='argument',
                           dependency_author_ids=(), strategy_id=None):
        """Import immutable authenticated P4 facts. Material evidence is controller
        attestation of progress/revision, not a worker Boolean or a content score.
        ``check_receipt`` is mandatory downstream for computable candidates; use
        register_check to distinguish computable claims before requesting review.
        """
        with self._transaction() as (s, now):
            require(s['phase'] != 'terminal', 'arm-terminal')
            require(purpose in ('allocation', 'completion'), 'invalid-purpose')
            require(claim_type in ('argument', 'computation', 'obstruction', 'reduction'), 'invalid-claim-type')
            require(set(dependency_author_ids) <= set(self.config['workers']), 'unknown-dependency-author')
            if purpose == 'allocation':
                self._research(s)
            identifier(candidate_id); digest(artifact_hash)
            require(obligation_id in s['obligations'], 'unknown-obligation')
            require(s['obligations'][obligation_id].get('purpose', 'allocation') != 'completion' or purpose == 'completion', 'completion-obligation-only')
            require(authors and set(authors) <= set(self.config['workers']), 'unknown-author')
            require(set(dependencies) <= set(s['candidates']), 'unknown-dependency')
            require(set(s['obligations'][obligation_id]['ancestry']) <= set(dependencies), 'lost-ancestry')
            require(purpose != 'completion' or full_scope is True, 'completion-scope-required')
            if progress_evidence is not None:
                identifier(progress_evidence)
            record = dict(id=candidate_id, obligation_id=obligation_id, lineage=s['obligations'][obligation_id]['lineage'],
                          artifact_hash=artifact_hash, authors=sorted(set(authors)), dependencies=sorted(set(dependencies)),
                          progress_evidence=progress_evidence, purpose=purpose, full_scope=full_scope,
                          check_receipt=check_receipt, computable=claim_type == 'computation',
                          dependency_author_ids=sorted(set(dependency_author_ids)),
                          strategy_id=identifier(strategy_id or obligation_id), strategy_explicit=strategy_id is not None)
            if check_receipt is not None:
                self._validate_check(artifact_hash, check_receipt)
            old = s['candidates'].get(candidate_id)
            if old:
                require(all(old[k] == v for k, v in record.items() if k != 'check_receipt' or v is not None), 'immutable-candidate-mismatch')
                return candidate_id
            record.update(created_ms=now, status='completion-unconfirmed' if purpose == 'completion' else 'provisional', helper_used=False)
            s['candidates'][candidate_id] = record
            self._event(s, 'register-candidate', candidate_id=candidate_id, artifact_hash=artifact_hash)
            return candidate_id

    @staticmethod
    def _validate_check(artifact_hash, receipt):
        require(isinstance(receipt, dict) and receipt.get('artifact_hash') == artifact_hash and
                receipt.get('command') and receipt.get('inputs_hash') and receipt.get('scope'), 'incomplete-check')
        digest(receipt['inputs_hash'])
        require(type(receipt.get('passed')) is bool, 'invalid-check-outcome')

    def register_check(self, candidate_id, *, receipt):
        """Trusted runner receipt: exact claim hash, pinned inputs, scope and command.
        A failed check suspends; a successful check proves only its recorded scope.
        """
        with self._transaction() as (s, now):
            c = s['candidates'][candidate_id]
            require(not any(p['candidate_id'] == candidate_id for p in s['pairs'].values()), 'check-after-review')
            self._validate_check(c['artifact_hash'], receipt)
            c['computable'], c['check_receipt'] = True, dict(receipt)
            if not receipt['passed']:
                self._invalidate(s, candidate_id, 'failed-check:' + candidate_id, True)
            self._event(s, 'reproducible-check', candidate_id=candidate_id, receipt=receipt)

    def _ancestry(self, s, candidate_id):
        visited, todo = set(), [candidate_id]
        while todo:
            cid = todo.pop()
            if cid not in visited:
                visited.add(cid)
                todo.extend(s['candidates'][cid]['dependencies'])
        return visited

    def _valid(self, s, candidate_id):
        return not any(s['invalid'].get(cid) for cid in self._ancestry(s, candidate_id))

    def _occupied(self, s):
        return (set(s['busy']) | {t['worker'] for t in s['tickets'].values()
            if not t.get('slot_released', False) and t.get('worker')} |
            {a['worker'] for a in list(s['helpers'].values()) + list(s['assignments'].values())
             if not a.get('slot_released', False)})

    def _review_workers(self, s, c, costs):
        staff = self._staffing(s)
        require(len(staff['active']) >= 3, 'no-two-worker-promotion')
        deps = self._ancestry(s, c['id']) - {c['id']}
        dep_authors = ({a for cid in deps for a in s['candidates'][cid]['authors']} |
                       set(c['dependency_author_ids']))
        excluded = set(c['authors']) | set(staff['explorers']) | self._occupied(s)
        eligible = [w for w in staff['active'] if w not in excluded and w in costs and
                    0 < integer(costs[w]) <= self.rules['review_ticket_microusd'] and
                    self._available(s, self._pool(self.config['workers'][w], 'review', s)) >= costs[w]]
        families = {w: self.contracts['models'][m]['family'] for w, m in self.config['workers'].items()}
        author_families = {families[a] for a in c['authors']}
        rotation = sorted(self.config['workers'], key=self._rank)
        offset = s['review_cursor'] % len(rotation)
        rotation = rotation[offset:] + rotation[:offset]
        pairs = []
        for a, b in combinations(eligible, 2):
            if a in dep_authors and b in dep_authors:
                continue
            if self.config['strategy'] == 'mixed-swarm' and families[a] == families[b]:
                continue
            if self.config['workers'][a] == self.config['workers'][b] and self._available(s, self._pool(self.config['workers'][a], 'review', s)) < costs[a] + costs[b]:
                continue
            pair = sorted((a, b), key=rotation.index)
            pairs.append((sum(families[w] in author_families for w in pair), tuple(rotation.index(w) for w in pair), pair))
        require(pairs, 'no-funded-eligible-pair')
        return min(pairs)[2], dep_authors

    def request_review(self, candidate_id, *, costs, exposure=None):
        """Costs are refreshed P3 worst-case request+tool bounds by worker identity.
        Returns intents for P3 ticket subaccounts/P4 sealed sessions, not a send.
        """
        with self._transaction() as (s, now):
            require(s['phase'] != 'terminal', 'arm-terminal')
            c = s['candidates'][candidate_id]
            if c['purpose'] == 'allocation':
                self._research(s)
            require(self._valid(s, candidate_id), 'invalid-dependency')
            require(not c['computable'] or (c['check_receipt'] and c['check_receipt']['passed']), 'missing-reproducible-check')
            lineage = s['lineages'][c['lineage']]
            require(lineage['pairs'] < self.rules['review_pairs_per_lineage'], 'lineage-review-exhausted')
            previous = [p for p in s['pairs'].values() if p['lineage'] == c['lineage']]
            require(not any(p['state'] == 'pending' for p in previous), 'lineage-review-active')
            if previous:
                require(c['progress_evidence'] and all(s['candidates'][p['candidate_id']]['artifact_hash'] != c['artifact_hash'] and
                    s['candidates'][p['candidate_id']]['progress_evidence'] != c['progress_evidence'] for p in previous), 'material-progress-required')
            require(sum(t['kind'] in ('allocation', 'completion', 'challenge') for t in s['tickets'].values()) + 2 <= self.policy['review_ticket_limit'], 'review-tickets-exhausted')
            reviewers, dep_authors = self._review_workers(s, c, costs)
            pair_id = self._id(s, 'pair')
            deadline = min(now + self.rules['review_ticket_seconds'] * 1000, self.config['deadline_ms'], s['closing_deadline_ms'] or self.config['deadline_ms'])
            ticket_ids = []
            for i, worker in enumerate(reviewers):
                model = self.config['workers'][worker]
                pool = self._pool(model, 'review', s)
                self._commit(s, pool, costs[worker])
                tid = pair_id + ':' + str(i)
                s['tickets'][tid] = dict(id=tid, kind=c['purpose'], worker=worker, model=model,
                    pool=pool, cap_microusd=costs[worker], deadline_ms=deadline, state='active',
                    max_input_tokens=64000, max_completion_tokens=64000, role='verifier' if c['purpose'] == 'completion' else 'reviewer')
                ticket_ids.append(tid)
            pair = dict(id=pair_id, candidate_id=candidate_id, candidate_hash=c['artifact_hash'],
                        lineage=c['lineage'], reviewers=reviewers, ticket_ids=ticket_ids,
                        deadline_ms=deadline, state='pending', purpose=c['purpose'],
                        dependency_authors=sorted(dep_authors), exposure={w: (exposure or {}).get(w, []) for w in reviewers})
            s['pairs'][pair_id] = pair
            lineage['pairs'] += 1
            s['review_cursor'] += 1
            self._event(s, 'review-pair', **pair)
            return dict(pair, tickets=[s['tickets'][t] for t in ticket_ids])

    def begin_pair_handoff(self, pair_id):
        """Consume the one external handoff attempt before calling P4. If a crash
        makes its outcome unknown, keep the pair consumed until its old expiry.
        """
        with self._transaction() as (s, now):
            p = s['pairs'][pair_id]
            require(p['state'] == 'pending', 'review-not-active')
            require(not p.get('handoff_state'), 'handoff-already-attempted')
            p['handoff_state'] = 'started'
            self._event(s, 'pair-handoff-started', pair_id=pair_id)

    def record_pair_handoff(self, pair_id, *, board_pair_id, tokens):
        """Controller-private durable role credentials; never mount this DB in a worker."""
        with self._transaction() as (s, now):
            p = s['pairs'][pair_id]
            require(p['state'] == 'pending' and p.get('handoff_state') == 'started', 'handoff-not-pending')
            require(set(tokens) == set(p['reviewers']), 'wrong-review-tokens')
            p.update(handoff_state='ready', board_pair_id=str(board_pair_id), private_tokens=dict(tokens))
            self._event(s, 'pair-handoff-ready', pair_id=pair_id, board_pair_id=str(board_pair_id))

    def finish_review(self, pair_id, *, reviews, board_pair_id):
        """P4's authenticated jointly revealed records only; never individual votes.
        Each record has reviewer, candidate_hash, verdict, relevant, scope, evidence.
        """
        with self._transaction() as (s, now):
            p = s['pairs'][pair_id]
            require(p['state'] == 'pending' and now < p['deadline_ms'], 'review-not-active')
            require(s['phase'] != 'terminal' and (s['phase'] != 'delivery' or p['purpose'] == 'completion'), 'review-phase-closed')
            identifier(board_pair_id)
            require(len(reviews) == 2 and {r['reviewer'] for r in reviews} == set(p['reviewers']), 'wrong-reviewers')
            for r in reviews:
                require(r['candidate_hash'] == p['candidate_hash'] and r['verdict'] in ('support', 'challenge', 'indeterminate') and
                        type(r['relevant']) is bool and r.get('scope') and r.get('evidence'), 'invalid-review-record')
            c = s['candidates'][p['candidate_id']]
            supported = all(r['verdict'] == 'support' and r['relevant'] for r in reviews) and self._valid(s, c['id'])
            c['status'] = ('peer-supported-completion' if p['purpose'] == 'completion' else 'peer-supported-for-allocation') if supported else ('completion-unconfirmed' if p['purpose'] == 'completion' else 'provisional')
            p.update(state='complete', board_pair_id=board_pair_id, reviews=reviews)
            for tid in p['ticket_ids']:
                s['tickets'][tid]['state'] = 'complete'
            if any(r['verdict'] == 'challenge' for r in reviews):
                self._invalidate(s, c['id'], 'review:' + pair_id, False)
            self._event(s, 'review-complete', pair_id=pair_id, status=c['status'])
            return c['status']

    def _invalidate(self, s, cid, cause, permanent):
        causes = s['invalid'].setdefault(cid, {})
        if cause in causes:
            require(causes[cause] == permanent, 'invalidation-mismatch')
            return
        causes[cause] = permanent
        for candidate in s['candidates'].values():
            if cid in self._ancestry(s, candidate['id']):
                candidate['status'] = 'suspended'
        for assignment in list(s['helpers'].values()) + list(s['assignments'].values()):
            deps = assignment.get('dependencies', [])
            if any(cid in self._ancestry(s, d) for d in deps) and assignment['state'] == 'active':
                assignment['state'] = 'suspended'
        self._event(s, 'invalidate', candidate_id=cid, cause=cause, permanent=permanent)

    def invalidate(self, candidate_id, *, evidence_id):
        """Trusted deterministic counterevidence or withdrawal, not free-form dissent."""
        with self._transaction() as (s, now):
            require(candidate_id in s['candidates'], 'unknown-candidate')
            self._invalidate(s, candidate_id, identifier(evidence_id), True)

    def request_challenge(self, candidate_id, *, worker, cost_microusd, evidence_id):
        with self._transaction() as (s, now):
            self._research(s)
            require(worker in self._staffing(s)['active'], 'unfunded-worker')
            require(worker not in self._staffing(s)['explorers'] and worker not in self._occupied(s), 'protected-or-busy-worker')
            require(not any(c['worker'] == worker and c['state'] == 'outstanding' for c in s['challenges'].values()), 'challenge-already-outstanding')
            c = s['candidates'][candidate_id]
            require(not any(ch['evidence_id'] == evidence_id for ch in s['challenges'].values()), 'duplicate-challenge')
            require(len(s['challenges']) < self.rules['challenge_' + self.config['profile'] + '_limit'], 'challenge-arm-exhausted')
            require(s['lineages'][c['lineage']]['challenges'] < self.rules['challenge_per_lineage'], 'challenge-lineage-exhausted')
            require(sum(t['kind'] in ('allocation', 'completion', 'challenge') for t in s['tickets'].values()) < self.policy['review_ticket_limit'], 'review-tickets-exhausted')
            require(0 < integer(cost_microusd) <= self.rules['review_ticket_microusd'], 'challenge-cost')
            tid = self._id(s, 'challenge')
            model = self.config['workers'][worker]
            pool = self._pool(model, 'review', s)
            self._commit(s, pool, cost_microusd)
            ticket = dict(id=tid, kind='challenge', worker=worker, model=model, pool=pool,
                          cap_microusd=cost_microusd, deadline_ms=min(now + 1800000, self.config['deadline_ms']), state='active',
                          max_input_tokens=64000, max_completion_tokens=64000, role='reviewer')
            s['tickets'][tid] = ticket
            s['challenges'][tid] = dict(worker=worker, candidate_id=candidate_id, evidence_id=identifier(evidence_id), state='outstanding')
            s['lineages'][c['lineage']]['challenges'] += 1
            self._invalidate(s, candidate_id, tid, False)
            self._event(s, 'challenge-admitted', ticket_id=tid)
            return ticket

    def resolve_challenge(self, ticket_id, *, sustained, resolution_evidence):
        with self._transaction() as (s, now):
            ch = s['challenges'][ticket_id]
            require(ch['state'] == 'outstanding', 'challenge-resolved')
            require(s['tickets'][ticket_id]['state'] == 'active', 'challenge-expired-unresolved')
            require(type(sustained) is bool, 'invalid-challenge-outcome')
            ch.update(state='resolved', resolution_evidence=identifier(resolution_evidence))
            s['tickets'][ticket_id]['state'] = 'complete'
            cid = ch['candidate_id']
            if sustained:
                s['invalid'][cid][ticket_id] = True
            else:
                del s['invalid'][cid][ticket_id]
                self._refresh_statuses(s)
                # Old assignments stay suspended until explicitly rechecked.
            self._event(s, 'challenge-resolved', ticket_id=ticket_id, sustained=sustained)

    def _refresh_statuses(self, s):
        for c in s['candidates'].values():
            if not self._valid(s, c['id']):
                c['status'] = 'suspended'
                continue
            pairs = [p for p in s['pairs'].values() if p['candidate_id'] == c['id'] and p['state'] == 'complete']
            p = pairs[-1] if pairs else None
            supported = p and all(r['verdict'] == 'support' and r['relevant'] for r in p['reviews'])
            c['status'] = ('peer-supported-completion' if c['purpose'] == 'completion' else 'peer-supported-for-allocation') if supported else ('completion-unconfirmed' if c['purpose'] == 'completion' else 'provisional')

    def sync_board_validity(self, candidate_id, *, valid):
        """Mirror authenticated P4 validity without declaring an unverified objection
        permanent. P4 alone resolves its causes; local permanent invalidations stay.
        """
        require(type(valid) is bool, 'invalid-validity')
        with self._transaction() as (s, now):
            require(candidate_id in s['candidates'], 'unknown-candidate')
            cause = 'board-validity:' + candidate_id
            if not valid:
                self._invalidate(s, candidate_id, cause, False)
            elif cause in s['invalid'].get(candidate_id, {}):
                del s['invalid'][candidate_id][cause]
                self._refresh_statuses(s)
                self._event(s, 'board-validity-restored', candidate_id=candidate_id)

    def recheck_assignment(self, assignment_id, *, evidence_id):
        """Resume only the SAME suspended envelope after its dependencies clear.
        No new helper permission, money, deadline or owner is minted.
        """
        with self._transaction() as (s, now):
            self._research(s)
            a = s['helpers'].get(assignment_id) or s['assignments'][assignment_id]
            require(a['state'] == 'suspended' and now < a['deadline_ms'], 'assignment-not-resumable')
            require(all(self._valid(s, d) for d in a['dependencies']), 'invalid-dependency')
            staff = self._staffing(s)
            require(a['worker'] in staff['active'], 'unfunded-worker')
            if a['kind'] == 'helper':
                require(a['worker'] not in staff['explorers'] + staff['challengers'], 'protected-slot')
            a['state'] = 'active'
            self._event(s, 'assignment-rechecked', assignment_id=assignment_id, evidence_id=identifier(evidence_id))
            return dict(a)

    def _free_worker(self, s, worker, *, helper):
        staff = self._staffing(s)
        require(worker in staff['active'] and worker not in self._occupied(s), 'unavailable-worker')
        if helper:
            require(worker not in staff['explorers'] + staff['challengers'], 'protected-slot')
        require(not any(a['worker'] == worker and a['state'] in ('active', 'cancel-required', 'suspended') for a in list(s['helpers'].values()) + list(s['assignments'].values())), 'assignment-active')

    def assign_independent(self, worker, *, obligation_id, cost_microusd, duration_seconds, initial=False):
        with self._transaction() as (s, now):
            self._research(s)
            self._free_worker(s, worker, helper=False)
            require(obligation_id in s['obligations'], 'unknown-obligation')
            require(type(initial) is bool, 'invalid-initial')
            cap, duration = self.rules['independent_assignment_microusd'], self.rules['independent_assignment_seconds']
            if initial:
                require(not any(a['worker'] == worker for a in s['assignments'].values()), 'initial-already-used')
                if self.config['strategy'] == 'dsf-swarm':
                    require(s['phase'] == 'breadth', 'breadth-closed')
                    cap = self.policy['pools_microusd']['breadth'] // 8
                    duration = self.policy['breadth_stop_seconds']
            require(0 < integer(cost_microusd) <= cap and 0 < integer(duration_seconds) <= duration, 'assignment-bound')
            deps = s['obligations'][obligation_id]['ancestry']
            require(all(self._valid(s, d) for d in deps), 'invalid-dependency')
            return self._assignment(s, now, worker, obligation_id, cost_microusd, duration_seconds, deps, 'independent')

    def _assignment(self, s, now, worker, obligation, cost, duration, deps, kind):
        require(not any(a['obligation_id'] == obligation and a['state'] in ('active', 'suspended', 'cancel-required') for a in list(s['helpers'].values()) + list(s['assignments'].values())), 'obligation-leased')
        require(s['obligations'][obligation].get('purpose', 'allocation') == 'allocation', 'completion-obligation-only')
        model = self.config['workers'][worker]
        pool = self._pool(model, 'research', s)
        self._commit(s, pool, cost)
        aid = self._id(s, kind)
        deadline = min(now + duration * 1000, self.config['deadline_ms'])
        if s['phase'] == 'breadth':
            deadline = min(deadline, self.config['started_at_ms'] + self.policy['breadth_stop_seconds'] * 1000)
        elif s['phase'] == 'continuation':
            deadline = min(deadline, self.config['started_at_ms'] + self.policy['continuation_absolute_stop_seconds'] * 1000,
                           s['continuation_started_ms'] + self.policy['continuation_max_seconds'] * 1000)
        if 'research_stop_seconds' in self.policy:
            deadline = min(deadline, self.config['started_at_ms'] + self.policy['research_stop_seconds'] * 1000)
        staff = self._staffing(s)
        work_kind = ('explorer' if worker in staff['explorers'] else
                     'challenger' if worker in staff['challengers'] else kind)
        record = dict(id=aid, worker=worker, obligation_id=obligation, model=model, pool=pool,
                      role='research', work_kind=work_kind,
                      cap_microusd=cost, deadline_ms=deadline, dependencies=deps, state='active', kind=kind)
        s['helpers' if kind == 'helper' else 'assignments'][aid] = record
        self._event(s, kind + '-assignment', **record)
        return record

    def assign_helper(self, worker, *, requests):
        """Authenticated posted help requests: id, candidate_id, obligation_id,
        strategy_id, contribution, completion_condition, cost_microusd. Select by
        least prior strategy helper commitment, oldest milestone, then stable ID.
        """
        with self._transaction() as (s, now):
            self._research(s)
            require(s['phase'] != 'breadth', 'helpers-after-breadth')
            self._free_worker(s, worker, helper=True)
            eligible = []
            cap = self.rules['helper_' + self.config['profile'] + '_microusd']
            for request in requests:
                c = s['candidates'].get(request['candidate_id'])
                if not c or c['status'] != 'peer-supported-for-allocation' or c['helper_used'] or not self._valid(s, c['id']):
                    continue
                obligation = request['obligation_id']
                require(obligation in s['obligations'] and obligation != c['obligation_id'], 'distinct-help-obligation-required')
                require(c['id'] in s['obligations'][obligation]['ancestry'], 'help-ancestry-required')
                deps = s['obligations'][obligation]['ancestry']
                if not all(self._valid(s, d) for d in deps):
                    continue
                if any(a['obligation_id'] == obligation and a['state'] in ('active', 'suspended', 'cancel-required') for a in list(s['helpers'].values()) + list(s['assignments'].values())):
                    continue
                if any(a.get('milestone_hash') == c['artifact_hash'] or (a.get('lineage') == c['lineage'] and (not c['progress_evidence'] or a.get('progress_evidence') == c['progress_evidence'])) for a in s['helpers'].values()):
                    continue
                require(request.get('contribution') and request.get('completion_condition'), 'incomplete-help')
                require(0 < integer(request['cost_microusd']) <= cap, 'helper-cost')
                identifier(request['id']); identifier(request['strategy_id'])
                require(not c['strategy_explicit'] or request['strategy_id'] == c['strategy_id'], 'strategy-identity-mismatch')
                # Legacy controller imports without a strategy use the stable
                # assigned obligation; a help author's label cannot reset it.
                request = dict(request, strategy_id=c['strategy_id'])
                pool = self._pool(self.config['workers'][worker], 'research', s)
                if self._available(s, pool) >= request['cost_microusd']:
                    eligible.append((s['helper_spend'].get(request['strategy_id'], 0), c['created_ms'], request['id'], request))
            require(eligible, 'no-eligible-help')
            request = min(eligible, key=lambda item: item[:3])[3]
            c = s['candidates'][request['candidate_id']]
            # Same artifact or unchanged progress can never mint another grant.
            require(not any(a.get('milestone_hash') == c['artifact_hash'] or
                    (a.get('lineage') == c['lineage'] and (not c['progress_evidence'] or a.get('progress_evidence') == c['progress_evidence']))
                    for a in s['helpers'].values()), 'additional-progress-required')
            record = self._assignment(s, now, worker, request['obligation_id'], request['cost_microusd'],
                        self.rules['helper_' + self.config['profile'] + '_seconds'],
                        s['obligations'][request['obligation_id']]['ancestry'], 'helper')
            record.update(milestone_hash=c['artifact_hash'], candidate_id=c['id'], lineage=c['lineage'],
                          progress_evidence=c['progress_evidence'], help_id=request['id'])
            c['helper_used'] = True
            strategy = request['strategy_id']
            s['helper_spend'][strategy] = s['helper_spend'].get(strategy, 0) + request['cost_microusd']
            return record

    def cancel_intent(self, intent_id, *, reason):
        """Close an intent's admission without releasing its physical slot.

        Runtime uses this for a failed/retiring owner. Only a later owned-process
        fence may release it; neither commitments nor original clocks change.
        """
        identifier(reason)
        with self._transaction() as (s, now):
            item = next((s[k][intent_id] for k in ('tickets','helpers','assignments')
                         if intent_id in s[k]), None)
            require(item is not None, 'unknown-intent')
            if item['state'] in ('active','suspended'):
                item['state'] = 'cancel-required'
                self._event(s, 'intent-cancel', intent_id=intent_id, reason=reason)

    def finish_assignment(self, assignment_id):
        """Controller confirms safe boundary/fencing; no cap or permission refund."""
        with self._transaction() as (s, now):
            a = s['helpers'].get(assignment_id) or s['assignments'][assignment_id]
            a['state'] = 'complete'
            a['slot_released'] = True
            self._event(s, 'assignment-finished', assignment_id=assignment_id)

    def fence_ticket(self, ticket_id, *, proof):
        """P2/P3 confirms the old process/request is ended or fenced. Expiry alone
        never frees a physical slot, removes a liability, or authorizes a retry.
        """
        with self._transaction() as (s, now):
            t = s['tickets'][ticket_id]
            require(t['state'] != 'active', 'ticket-still-active')
            t['slot_released'] = True
            t['fence_proof'] = identifier(proof)
            if t['kind'] == 'glm':
                s['glm_active'] = None
            self._event(s, 'ticket-fenced', ticket_id=ticket_id, proof=proof)

    def start_glm(self, *, obligation_id, packet_hash, input_tokens, worst_case_microusd):
        with self._transaction() as (s, now):
            self._research(s)
            require(self.config['strategy'] == 'mixed-swarm', 'glm-not-in-arm')
            require(now < self.config['started_at_ms'] + self.policy['glm_last_admission_seconds'] * 1000, 'glm-admission-cutoff')
            require(s['glm_active'] is None and not any(t['kind'] == 'glm' and not t.get('slot_released', False) for t in s['tickets'].values()), 'glm-already-active')
            require(s['glm_failures'] < self.rules['glm_empty_or_exhausted_limit'], 'glm-failure-limit')
            require(sum(t['kind'] == 'glm' for t in s['tickets'].values()) < self.policy['glm_ticket_limit'], 'glm-ticket-limit')
            require(obligation_id in s['obligations'], 'unknown-obligation')
            require(s['obligations'][obligation_id].get('purpose', 'allocation') == 'allocation', 'completion-obligation-only')
            require(integer(input_tokens) <= 64000, 'oversized-glm-packet')
            require(0 < integer(worst_case_microusd) <= self.rules['glm_ticket_microusd'], 'glm-request-cost')
            digest(packet_hash)
            tid = self._id(s, 'glm')
            cap = self.rules['glm_ticket_microusd']
            self._commit(s, 'glm:research', cap)
            ticket = dict(id=tid, kind='glm', worker=None, model='glm', pool='glm:research', role='specialist',
                          cap_microusd=cap, deadline_ms=min(now + 3600000, self.config['deadline_ms'],
                          self.config['started_at_ms'] + self.policy.get('research_stop_seconds', self.policy['wall_seconds']) * 1000),
                          state='active', packet_hash=packet_hash, obligation_id=obligation_id,
                          max_input_tokens=64000, max_completion_tokens=128000, transport_retries=0,
                          committed_microusd=worst_case_microusd, request_active=True,
                          capabilities=['read-frozen-packet', 'return-specialist-result'])
            s['tickets'][tid] = ticket
            s['glm_active'] = tid
            self._event(s, 'glm-ticket', **ticket)
            return ticket

    def glm_transport_retry(self, ticket_id, *, worst_case_microusd):
        with self._transaction() as (s, now):
            self._research(s)
            t = s['tickets'][ticket_id]
            require(t['kind'] == 'glm' and t['state'] == 'active' and not t['request_active'], 'glm-retry-not-ready')
            require(t.get('last_outcome') == 'transport-error' and t['transport_retries'] < 1, 'glm-retry-exhausted')
            require(0 < integer(worst_case_microusd) <= t['cap_microusd'] - t['committed_microusd'], 'glm-ticket-cost')
            t['committed_microusd'] += worst_case_microusd
            t['transport_retries'] += 1
            t['request_active'] = True
            self._event(s, 'glm-transport-retry', ticket_id=ticket_id)
            return t

    def finish_glm(self, ticket_id, *, outcome, result_hash=None):
        with self._transaction() as (s, now):
            t = s['tickets'][ticket_id]
            require(t['kind'] == 'glm' and t['state'] == 'active' and t['request_active'], 'glm-not-active')
            require(outcome in ('candidate', 'objection', 'abstention', 'empty', 'output-truncated', 'reasoning-exhausted', 'transport-error', 'timed-out'), 'invalid-glm-outcome')
            if outcome in ('candidate', 'objection'):
                digest(result_hash)
            t.update(last_outcome=outcome, request_active=False, result_hash=result_hash)
            if outcome == 'transport-error' and t['transport_retries'] < 1:
                self._event(s, 'glm-transport-error', ticket_id=ticket_id)
                return 'retry-eligible'
            if outcome in ('empty', 'output-truncated', 'reasoning-exhausted'):
                s['glm_failures'] += 1
            t['state'] = 'complete'
            s['glm_active'] = None
            self._event(s, 'glm-finished', ticket_id=ticket_id, outcome=outcome)
            return 'unreviewed-handoff' if outcome in ('candidate', 'objection') else outcome
