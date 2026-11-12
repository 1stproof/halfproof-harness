"""Comparable operational outcomes, never a ranking by raw accepts."""
def score(snapshot):
    progress=snapshot.get('progress') or {}
    supported=set(progress.get('current_supported_reasoning',[]))
    costs=snapshot.get('stage_costs') or {}
    settled=sum(row.get('settled_microusd',0) for row in costs.values())/1e6
    elapsed=max(0,snapshot.get('observed_at',snapshot.get('deadline',0))-snapshot.get('original_start',0))/3600
    checks=(snapshot.get('executor') or {}).get('checks') or {}
    adverse=sum(report.get('status')!='internal-supported' for check in checks.values() for report in check.get('reports',{}).values())
    return dict(current_checked_handoffs=len(supported),settled_dollars=settled,elapsed_hours=elapsed,
                checked_handoffs_per_settled_dollar=len(supported)/settled if settled else None,
                checked_handoffs_per_hour=len(supported)/elapsed if elapsed else None,
                adverse_final_audits=adverse,stalled_stages=progress.get('stalled_stages',[]),
                mathematical_completion='ungraded',allocation_tuning='requires controlled same-problem comparison')
