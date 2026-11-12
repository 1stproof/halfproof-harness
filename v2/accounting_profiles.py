"""Translate P1 immutable profiles into P3 accounts and wire records."""
import json
from pathlib import Path
from .accounting import AdmissionContext, AdmissionDenied, LOOKUP_DELAYS, canonical
from .contracts_v3 import load_contracts, contracts_sha256


def _contract_directory(directory):
    adjacent=Path(__file__).resolve().parent/'contracts'
    return directory or (adjacent if adjacent.is_dir() else None)


def configure_cohort(store, *, cohort, problem, profile, starts, expected_contracts_sha256,
                     route_layouts, contracts_directory=None):
    """Register the complete fixed cohort; route_layouts must be qualified by P9.

    starts maps each arm ID to its original absolute start time. IDs are the P1
    strategy names within this per-problem store. No default provider capacity is
    certified merely by calling this function.
    """
    directory=_contract_directory(contracts_directory)
    policy = load_contracts(directory, expected_sha256=expected_contracts_sha256)
    if profile not in ('production','pilot','swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue') or set(starts) != set(policy[profile]):
        raise ValueError('complete cohort registration required')
    arms = policy[profile]
    store.register_cohort(cohort, problem=problem, profile=profile,
        cap=policy[profile+'_total_microusd'], arm_caps={a:r['cap_microusd'] for a,r in arms.items()},
        recovery_reserve=policy['pilot_shared_recovery_microusd'] if profile=='pilot' else 0,
        contracts_sha256=expected_contracts_sha256,contracts_directory=directory)
    for arm, row in arms.items():
        start = starts[arm]
        end = start + row['wall_seconds']
        store.register_arm(arm,cap=row['cap_microusd'],start=start,deadline=end,
            models=[policy['models'][m]['model_id'] for m in row['models']],solo=arm.endswith('-solo'))
        research_end = min(end,start+row.get('research_stop_seconds',row['wall_seconds']))
        if row.get('orchestration') == 'stateless-v1':
            store.register_account(arm+'/research', parent=arm,
                cap=row['cap_microusd']-row['report_reserve_microusd'],
                purpose='dynamic-research', deadline=research_end)
            for name, cap in row['closing_reserves_microusd'].items():
                store.register_account(arm+'/'+name, parent=arm, cap=cap,
                    purpose='closing-'+name, deadline=end)
        elif 'model_pools_microusd' in row:
            for model, pools in row['model_pools_microusd'].items():
                parent=arm+'/'+model
                store.register_account(parent,parent=arm,cap=sum(pools.values()),purpose='model',model=policy['models'][model]['model_id'])
                for pool, cap in pools.items():
                    if cap:
                        purpose='specialist' if model=='glm' else pool
                        store.register_account(parent+'/'+pool,parent=parent,cap=cap,purpose=purpose,
                            deadline=research_end if pool=='research' else end)
            if row['recovery_microusd']:
                store.register_account(arm+'/recovery',parent=arm,cap=row['recovery_microusd'],purpose='recovery')
        else:
            for pool, cap in row['pools_microusd'].items():
                purpose='research' if pool in ('breadth','continuation','research_and_verification') else pool
                stop=research_end if purpose=='research' else end
                if pool=='breadth':stop=min(stop,start+row['breadth_stop_seconds'])
                if pool=='continuation':stop=min(stop,start+row['continuation_absolute_stop_seconds'])
                if arm.endswith('-solo') and pool == 'report':
                    # Split the existing reserve, never increase the arm cap.
                    verifier_cap = cap // 2
                    store.register_account(arm+'/final-verification', parent=arm,
                        cap=verifier_cap, purpose='verify', deadline=end)
                    cap -= verifier_cap
                store.register_account(arm+'/'+pool,parent=arm,cap=cap,purpose=purpose,deadline=stop)
    for model_key, layout in route_layouts.items():
        model=policy['models'].get(model_key)
        if model is None:raise ValueError('unknown route')
        for arm, slots in layout['arm_limits'].items():
            if arm not in arms or model_key not in arms[arm]['models'] or ('slots' in arms[arm] and slots > arms[arm]['slots'][model_key]):
                raise ValueError('route layout exceeds fixed worker slots')
        store.register_route(model['model_id'],**layout)
    with store.transaction() as db:
        db.execute("INSERT OR IGNORE INTO settings VALUES ('profile-configured',?)",(expected_contracts_sha256,))
        from .production_continuation import active
        continuation = active()
        if profile == 'production' and continuation and continuation.get('kind') == 'operator-fresh-p01-20261011':
            db.execute("INSERT OR IGNORE INTO settings VALUES ('operator-fresh-p01-hard-cap','mixed-swarm')")
    return expected_contracts_sha256


def preflight_author_reserves(store, *, prices, input_tokens=196608, output_tokens=128000, contracts_directory=None):
    """Fail before research if a REQUIRED final author's full request cannot fit.

    prices maps exact model IDs to refreshed (worst_input_per_token,
    worst_output_per_token) dollars. Include cache-write premiums in input price.
    Optional mixed contributions are not promised a request by this gate.
    """
    from decimal import Decimal
    from .accounting import micros, integer
    integer(input_tokens,positive=True);integer(output_tokens,positive=True)
    policy=load_contracts(_contract_directory(contracts_directory))
    with store.transaction() as db:
        cohort=db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()
        if not cohort:raise AdmissionDenied('cohort-unregistered')
        registration=json.loads(cohort[0])
        for arm,row in policy[registration['profile']].items():
            author=row['final_author']
            model=policy['models'][author]['model_id']
            if model not in prices:raise AdmissionDenied('price-unverified')
            ip,op=(Decimal(str(x)) for x in prices[model])
            if not ip.is_finite() or not op.is_finite() or min(ip,op)<0:raise ValueError('invalid price')
            maximum=micros(input_tokens*ip+output_tokens*op)
            if arm.endswith('-solo'):
                from .solo_limits import INPUT_TOKENS, OUTPUT_TOKENS, RESERVED_REQUESTS
                from .output_limits import provider_max
                maximum=RESERVED_REQUESTS*micros(INPUT_TOKENS*ip+provider_max(model)*op)
            account=arm+'/'+author+'/report' if 'model_pools_microusd' in row else arm+'/report'
            if row.get('orchestration') == 'stateless-v1':
                account=arm+'/writer'
                shape=row['request_limits']
                from .output_limits import provider_max
                maximum=micros(shape['input_tokens']*ip+provider_max(model)*op)
                required={'control':row['orchestrator'], 'verifier-a':'dsf',
                          'verifier-b':'qwen24t' if arm=='mixed-swarm' else 'dsf'}
                for role, key in required.items():
                    mid=policy['models'][key]['model_id']
                    if mid not in prices:raise AdmissionDenied('price-unverified')
                    rip,rop=(Decimal(str(x)) for x in prices[mid])
                    bound=micros(shape['input_tokens']*rip+provider_max(mid)*rop)
                    # No closing reserves under the 2026-10-05 spend-then-assess ruling.
            allocated=db.execute('SELECT cap FROM accounts WHERE id=?',(account,)).fetchone()
            if not allocated:raise AdmissionDenied('required-author-reserve')
            # Spend-then-assess (2026-10-05): the author pool need not pre-hold a maximum-size request.
            if arm.endswith('-solo'):
                verifier=db.execute('SELECT cap FROM accounts WHERE id=?',(arm+'/final-verification',)).fetchone()
                if not verifier:raise AdmissionDenied('required-verifier-reserve')
        db.execute("INSERT OR REPLACE INTO settings VALUES ('author-reserves-preflight',?)",(canonical(dict(input_tokens=input_tokens,output_tokens=output_tokens)),))
    return True


def wire_records(store, arm, *, contracts_directory=None):
    """Export exact P1 records. A breached bound deliberately fails validation."""
    policy=load_contracts(_contract_directory(contracts_directory))
    model_keys={v['model_id']:k for k,v in policy['models'].items()}
    role_map={'review':'reviewer','verify':'verifier','recovery':'report'}
    with store.transaction() as db:
        cohort_row=db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()
        if not cohort_row:raise AdmissionDenied('cohort-unregistered')
        cohort=json.loads(cohort_row[0])
        registration=db.execute('SELECT * FROM arms WHERE id=?',(arm,)).fetchone()
        if not registration:raise ValueError('unknown arm')
        totals=store._totals(db,arm)
        rows=db.execute('SELECT * FROM attempts WHERE arm=? ORDER BY created,id',(arm,)).fetchall()
        attempts=[]
        for row in rows:
            ctx=json.loads(row['context'])
            key=model_keys[ctx['model']]
            due=(row['ended']+LOOKUP_DELAYS[row['lookups']] if row['state']=='unresolved' and
                 row['generation'] and row['ended'] is not None and row['lookups']<7 and
                 store.clock()<row['ended']+86400 else None)
            attempts.append(dict(contract_version=1,cohort_id=cohort['id'],problem_id=cohort['problem'],
                arm_id=arm,request_id=ctx['logical_request'] or row['id'],attempt_id=row['id'],worker_id=ctx['worker'],
                role=role_map.get(ctx['role'],ctx['role']),model_key=key,model_id=ctx['model'],provider=ctx['provider'],
                effort=policy['models'][key]['effort'],phase_id=ctx['phase'],pool_id=ctx['account'],ticket_id=ctx['ticket'],
                admitted_at_unix_ms=round(row['created']*1000),deadline_unix_ms=round(row['deadline']*1000),
                upper_bound_microusd=row['bound'],state=row['state'],settled_microusd=row['settled'],
                unresolved_upper_microusd=row['liability'],active_reservation_microusd=row['reservation'],
                estimate_microusd=row['estimate'],released_microusd=row['released'],generation_id=row['generation'],
                lookup_count=row['lookups'],next_lookup_unix_ms=round(due*1000) if due is not None else None,
                request_class=ctx['request_class']))
        cap=json.loads(registration['config'])['cap']
        grant=db.execute('SELECT COALESCE(SUM(amount),0) FROM grants WHERE arm=?',(arm,)).fetchone()[0]
        snapshot=dict(contract_version=1,cohort_id=cohort['id'],problem_id=cohort['problem'],arm_id=arm,
            sequence=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0],
            original_cap_microusd=cap,recovery_grant_microusd=grant,settled_microusd=totals['settled'],
            unresolved_upper_microusd=totals['liability'],active_reservation_microusd=totals['reservation'],
            available_microusd=max(0,cap+grant-totals['committed']),
            conservation_residual_microusd=store._audit(db) or totals['overrun'] or min(0,cap+grant-totals['committed']),
            attempt_ids=[r['id'] for r in rows])
        return dict(attempts=attempts,ledger_snapshot=snapshot)


class PublisherAcknowledgment:
    """P7 publisher adapter, used in the controller process, never forked.

    P7 already isolates durable storage. Its parent poll/require_ack are local and
    nonblocking. All callers share this adapter and the one cohort publisher.
    """
    _journal_guard=__import__('threading').Lock()

    def __init__(self, store, publisher):
        import threading
        self.store=store
        self.publisher=publisher
        # Every slot adapter shares one journal for this store/publisher. A new
        # controller starts a fresh base; old acknowledged chains remain readable.
        with self._journal_guard:
            journals = publisher.__dict__.setdefault('_accounting_journals', {})
            self.journal = journals.setdefault(str(store.path.resolve()),
                dict(lock=threading.RLock(), cursor=None, previous=None, digest=None, count=0, names=set()))
        self.lock=self.journal['lock']

    def begin(self, reservation):
        import hashlib
        from .accounting import canonical
        name='ledger-'+reservation['id']
        with self.lock:
            j=self.journal
            if name in j['names']:return name
            # Bound recovery chain length; full bases occur once per 128 sends.
            base = j['cursor'] is None or j['count'] >= 128
            cursor, value = self.store.publication_checkpoint(None if base else j['cursor'])
            if not base:
                value=dict(schema='accounting-delta-v1', after=j['cursor'], cursor=cursor,
                    previous=j['previous'], previous_sha256=j['digest'], changes=value)
            self.publisher.submit(name,value)
            j['names'].add(name)
            j.update(cursor=cursor, previous=name+'.json',
                digest=hashlib.sha256(canonical(value).encode()).hexdigest(), count=0 if base else j['count']+1)
        return name

    def acknowledged(self, name):
        with self.lock:return self.publisher.require_ack(name)
