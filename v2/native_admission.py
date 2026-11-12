"""Content-free native accounting admission state for launch fences and observers."""
def snapshot(store, arm):
    with store.transaction() as db:
        row=db.execute('SELECT mode,pause FROM arms WHERE id=?',(arm,)).fetchone()
        settings=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('operator-paused','blocked','provider-refusal')"))
        return dict(arm=arm,mode=row['mode'] if row else None,
                    arm_pause=row['pause'] if row else None,
                    provider_refusal=('provider-refusal' in settings or bool(getattr(store,'provider_account',None) and store.provider_account.snapshot()['paused'])),
                    operator_paused='operator-paused' in settings,
                    accounting_blocked='blocked' in settings,
                    known_arm=row is not None)


def closed(state):
    return (not state['known_arm'] or state['mode']=='terminal' or bool(state['arm_pause'])
            or state.get('provider_refusal') or state['operator_paused'] or state['accounting_blocked'])


def defects(state):
    if not state:return []  # old publications: no invented native evidence
    out=[]
    if state.get('provider_refusal'):out.append('provider account admission paused')
    if state.get('arm_pause'):out.append('native arm admission paused: '+str(state['arm_pause']))
    if state.get('accounting_blocked') and not state.get('operator_paused'):
        out.append('native accounting admission blocked')
    return out
