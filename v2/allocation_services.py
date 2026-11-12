"""Controller adapters for P3 accounts and P4 sealed reviews. No inference calls.

Import the actual accounting/board modules only in an integrated release. Every
method still needs P7 durability and P2 fresh role handover before paid dispatch.
"""
from .allocation import AllocationDenied, require


class AccountingBridge:
    def __init__(self, allocation, accounting):
        self.allocation = allocation
        self.accounting = accounting
        self.arm = allocation.config['arm_id']

    def pool_id(self, pool):
        return self.arm + ':pool:' + pool

    def register_pools(self):
        """Idempotently carve all pools before any direct arm admission can occur."""
        a, store = self.allocation, self.accounting
        cfg, policy = a.config, a.policy
        store.register_arm(self.arm, cap=policy['cap_microusd'], start=cfg['started_at_ms'] / 1000,
                           deadline=cfg['deadline_ms'] / 1000,
                           models=[a.contracts['models'][m]['model_id'] for m in policy['models']])
        if cfg['strategy'] == 'dsf-swarm':
            for pool, cap in policy['pools_microusd'].items():
                if cap:
                    store.register_account(self.pool_id(pool), parent=self.arm, cap=cap,
                                           purpose={'review': 'review', 'report': 'report', 'recovery': 'recovery'}.get(pool, 'research'),
                                           model=a.contracts['models']['dsf']['model_id'])
        else:
            for model, pools in policy['model_pools_microusd'].items():
                mid = a.contracts['models'][model]['model_id']
                model_account = self.arm + ':model:' + model
                store.register_account(model_account, parent=self.arm, cap=sum(pools.values()), purpose='model', model=mid)
                for pool, cap in pools.items():
                    if cap:
                        store.register_account(self.pool_id(model + ':' + pool), parent=model_account, cap=cap,
                                               purpose='specialist' if model == 'glm' else pool, model=mid)
            if policy['recovery_microusd']:
                store.register_account(self.pool_id('recovery'), parent=self.arm, cap=policy['recovery_microusd'], purpose='recovery')

    def observe_liability(self):
        """Feed actual P3 liabilities to phase stops; carved envelopes are not bills."""
        policy = self.allocation.policy
        if self.allocation.config['strategy'] == 'dsf-swarm':
            pools = list(policy['pools_microusd'])
        else:
            pools = [model + ':' + kind for model, kinds in policy['model_pools_microusd'].items()
                     for kind, cap in kinds.items() if cap]
        snapshot=self.accounting.totals_many([self.arm,*[self.pool_id(pool) for pool in pools]])
        amounts = {pool:snapshot[self.pool_id(pool)]['committed'] for pool in pools}
        self.allocation.observe_liability(pools=amounts, parent_microusd=snapshot[self.arm]['committed'])
        return amounts

    def provision(self, intent_id):
        """Use the persisted intent; caller cannot choose another cap/model/pool."""
        state = self.allocation.snapshot()
        item = next((state[key][intent_id] for key in ('tickets', 'helpers', 'assignments') if intent_id in state[key]), None)
        require(item is not None and item['state'] == 'active', 'intent-not-active')
        require(state['phase'] != 'terminal' and (state['phase'] != 'delivery' or item['kind'] == 'completion'), 'intent-phase-closed')
        model = self.allocation.contracts['models'][item['model']]['model_id']
        self.accounting.register_account(intent_id, parent=self.pool_id(item['pool']), cap=item['cap_microusd'],
                                         purpose='ticket', model=model, deadline=item['deadline_ms'] / 1000)
        return dict(item)

    def context(self, intent_id, *, credential_id):
        """Create P3's server-owned context; gateway reserves per-request cost later."""
        from .accounting import AdmissionContext
        item = self.provision(intent_id)
        route = self.allocation.contracts['models'][item['model']]
        role = {'reviewer': 'review', 'verifier': 'review'}.get(item['role'], item['role'])
        return AdmissionContext(arm=self.arm, account=intent_id, model=route['model_id'],
                                provider=route['provider'], credential=credential_id, role=role,
                                max_input_tokens=item.get('max_input_tokens', 196608),
                                max_output_tokens=item.get('max_completion_tokens', 128000))


class BoardBridge:
    def __init__(self, allocation, board, *, controller_secret, funding):
        self.allocation, self.board = allocation, board
        self.secret, self.funding = controller_secret, funding
        require(board.arm_id == allocation.config['arm_id'] and board.problem_id == allocation.config['problem_id'], 'board-scope-mismatch')

    @staticmethod
    def candidate_id(board_id):
        return 'board:' + str(board_id)

    def import_candidate(self, board_id, *, strategy_id, progress_evidence=None, runner_receipt=None):
        """Read authorship/lineage from the authenticated board, not model text.
        Strategy and material-progress attestations are fixed controller records.
        """
        facts = self.board.candidate_facts(self.secret, board_id)
        require(facts['valid'] and facts['completes_assignment'], 'board-candidate-ineligible')
        cid = self.candidate_id(board_id)
        state = self.allocation.snapshot()
        deps = [self.candidate_id(d) for d in facts['dependency_record_ids'] if self.candidate_id(d) in state['candidates']]
        obligation = 'board-lineage:' + str(facts['lineage_id'])
        if obligation not in state['obligations']:
            self.allocation.register_obligation(obligation, ancestry=deps, purpose=facts['purpose'])
        self.allocation.register_candidate(cid, obligation_id=obligation, artifact_hash=facts['artifact_hash'],
            authors=facts['author_ids'], dependencies=deps, dependency_author_ids=facts['dependency_author_ids'],
            progress_evidence=progress_evidence, purpose=facts['purpose'], full_scope=facts['purpose'] == 'completion',
            claim_type=facts['claim_type'], strategy_id=strategy_id)
        if facts['claim_type'] == 'computation' and runner_receipt is not None:
            self.allocation.register_check(cid, receipt=runner_receipt)
        return cid

    def open_pair(self, pair_id):
        """Persist a one-use handoff marker before external writes; unknown outcomes
        fail closed. A completed handoff is replayable with identical private tokens.
        """
        p = self.allocation.snapshot()['pairs'][pair_id]
        require(p['state'] == 'pending', 'review-not-active')
        if p.get('handoff_state') == 'ready':
            return dict(pair_id=p['board_pair_id'], candidate_hash=p['candidate_hash'], tokens=p['private_tokens'])
        require(p['candidate_id'].startswith('board:'), 'not-board-candidate')
        board_id = int(p['candidate_id'][6:])
        facts = self.board.candidate_facts(self.secret, board_id)
        require(facts['valid'] and facts['artifact_hash'] == p['candidate_hash'], 'board-candidate-ineligible')
        for tid in p['ticket_ids']:
            self.funding.provision(tid)
        self.allocation.begin_pair_handoff(pair_id)
        result = self.board.open_review_pair(self.secret, candidate_id=board_id,
            reviewers=p['reviewers'], deadline=p['deadline_ms'] / 1000, exposure=p['exposure'],
            ticket_ids=p['ticket_ids'], purpose=p['purpose'])
        require(result['candidate_hash'] == p['candidate_hash'], 'board-hash-mismatch')
        self.allocation.record_pair_handoff(pair_id, board_pair_id=result['pair_id'], tokens=result['tokens'])
        return result

    def finish_pair(self, pair_id):
        """Read only revealed P1 records; a single sealed vote cannot reach P5."""
        p = self.allocation.snapshot()['pairs'][pair_id]
        require(p.get('handoff_state') == 'ready', 'pair-not-handed-off')
        facts = self.board.candidate_facts(self.secret, int(p['candidate_id'][6:]))
        if not facts['valid']:
            self.allocation.sync_board_validity(p['candidate_id'], valid=False)
            raise AllocationDenied('board-candidate-invalid')
        wire = self.board.export_wire(self.secret)
        records = [r for r in wire if r.get('pair_id') == p['board_pair_id'] and r.get('ticket_id') in p['ticket_ids']]
        require(len(records) == 2, 'pair-not-jointly-revealed')
        board_status = self.board.candidate_status(self.secret, int(p['candidate_id'][6:]))
        if all(r['verdict'] == 'support' and r['relevance_supported'] for r in records):
            require(board_status in ('peer-supported-for-allocation', 'peer-supported-completion'), 'board-support-not-qualified')
        reviews = [dict(reviewer=r['reviewer_id'], candidate_hash=r['candidate_sha256'], verdict=r['verdict'],
                        relevant=r['relevance_supported'], scope=r['checked_scope'], evidence=r['evidence_pointers']) for r in records]
        return self.allocation.finish_review(pair_id, reviews=reviews, board_pair_id=p['board_pair_id'])

    def sync_invalidations(self):
        """Call before any helper/independent dispatch, then fence affected work."""
        for cid in self.allocation.snapshot()['candidates']:
            if cid.startswith('board:'):
                facts = self.board.candidate_facts(self.secret, int(cid[6:]))
                self.allocation.sync_board_validity(cid, valid=facts['valid'])
