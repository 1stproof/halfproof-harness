"""Independent, bounded opening proposals and asynchronous other-family critiques.

These model outputs are context, never claims or verdicts. No roster slots change.
"""
MODELS = ('k3', 'qwen24t', 'glm', 'dsp')
STAGES = ('opening-proposal', 'opening-critique')
SECONDS = 900
CAP_MICROUSD = 25_000_000
CRITIQUE_PROPOSAL_BYTES = 32_000


def critique_brief(text):
    raw = text.encode("utf-8")
    excerpt = raw[:CRITIQUE_PROPOSAL_BYTES].decode("utf-8", errors="ignore")
    if len(raw) > CRITIQUE_PROPOSAL_BYTES:
        excerpt += "\n[Proposal truncated by harness; critique only the supplied excerpt.]"
    return CRITIQUE + excerpt

PROPOSE = ('Read PROBLEM.md. Independently propose several genuinely different approaches to the question. '
           'For each, explain what you would establish first and how you would tell that it is failing. '
           'These are proposals, not established claims. '
           'Return them in your final response. Finish within 15 minutes. You have no other team outputs.')
CRITIQUE = ('Read PROBLEM.md and the proposal below. Give an overall critique: assess whether the approach '
            'is sound, identify its weakest point, and explain what would make it fail. This is input to '
            'ongoing work, not a verdict or a gate. Return it in your final '
            'response within 15 minutes.\n\nProposal:\n')


def enabled(runtime):
    return runtime.strategy == 'mixed-swarm' and not runtime.config.get('open_swarm')


def start(runtime):
    if not enabled(runtime):
        runtime.executor.publish_event('startup', 'startup')
        return
    with runtime.executor.tx() as (_, state):
        state['opening_pending'] = True
        state['opening_sessions'] = {m: {'state': 'pending'} for m in MODELS}
    for model in MODELS:
        make_slot(runtime, 'opening:'+model, model, STAGES[0], PROPOSE)


def make_slot(runtime, identity, model, stage, brief, proposal=None):
    w = runtime._new_slot(identity, model=model, role='specialist', stage=stage,
                          account='research', deadline=min(runtime.deadline, runtime.clock()+SECONDS),
                          packet={'proposal': proposal}, brief=brief,
                          shape=dict(input_tokens=64000))
    # No continuation evidence or shared board is visible to an independent proposer.
    for name in getattr(runtime, 'seed_files', []):
        (w.work/name).unlink(missing_ok=True)
    w.publication = runtime.checkpoint()
    return w


def finish(runtime, w, failure=None):
    text = w.state.get('response_text') or ''
    path = w.work/'APPROACHES.md'
    if path.is_file():
        text = path.read_text(errors='replace')[:240000] or text
    proposal = w.packet.get('proposal')
    kind = 'approaches' if w.stage == STAGES[0] else 'approach-critique'
    record = runtime._publish(identity=w.slot_id, kind=kind,
        body=dict(worker=w.slot_id, model=w.run.lane, proposal=proposal, text=text,
                  failure=failure, seconds=SECONDS, cap_microusd=CAP_MICROUSD,
                  status='proposal' if text else 'failed', claim=False))
    if record is None:
        # _publish has logged the failure; retain local output and close the slot.
        # Treat an unpublished proposal as failed so all-failed startup can recover.
        text = ''
        failure = 'opening-output-publication-failed'
        w.state['slot_failure'] = failure
    if w.stage == STAGES[0]:
        with runtime.executor.tx() as (_, state):
            state['opening_sessions'][w.run.lane] = dict(state='completed' if text else 'failed', record=record, failure=failure)
            if text or all(r['state'] != 'pending' for r in state['opening_sessions'].values()):
                state['opening_pending'] = False
        if text:
            # Deterministic cross-family assignment; independent of ordinary research roster.
            critic = 'glm' if w.run.lane != 'glm' else 'k3'
            runtime.executor.publish_event('approach:'+str(record), 'approach', record=record,
                artifact=next(x for x in runtime._board_packet()['evidence'] if x['id']=='board:'+str(record)))
            try:
                make_slot(runtime, 'approach-critique:'+str(record), critic, STAGES[1], critique_brief(text), record)
            except Exception as exc:
                runtime.__dict__.setdefault('slot_errors', []).append(dict(slot=w.slot_id,what='opening-critique',error=str(exc)[:300],at=runtime.clock()))
        elif all(r['state']=='failed' for r in runtime.executor.snapshot()['opening_sessions'].values()):
            runtime.executor.publish_event('opening-fallback', 'startup', reason='all proposers ended without output')
    elif text:
        runtime.executor.publish_event('approach-critique:'+str(record), 'approach-critique',
            proposal=proposal, artifact=next(x for x in runtime._board_packet()['evidence'] if x['id']=='board:'+str(record)))


def budget_check(db, ctx, bound):
    """Atomic settled + held + reserved admission cap, including transport retries."""
    if ctx.phase not in STAGES:
        return
    from .accounting import AdmissionDenied
    import json
    spent = sum(r['settled']+r['liability']+r['reservation']
                for r in db.execute('SELECT context,settled,liability,reservation FROM attempts WHERE arm=?', (ctx.arm,))
                if json.loads(r['context']).get('worker') == ctx.worker)
    if spent+bound > CAP_MICROUSD:
        raise AdmissionDenied('opening-session-budget')
