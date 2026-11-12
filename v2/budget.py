"""Operator ruling ~18:45 PDT: the arm budget is a planning figure, not a stop.

Research continues until SETTLED spend reaches budget x RESEARCH_OVERRUN
($250 on a $200 arm); closing, writing, critique, rewrite and vote always run
on top of that, uncapped. Overrun is reported per arm at freeze.
"""
RESEARCH_OVERRUN = 1.25


def research_limit(cap):
    return int(cap * RESEARCH_OVERRUN)


def financial_admission(cap, totals, *, bound=0, research=False, hard=False):
    """Same arithmetic as admission, excluding route/deadline/capability checks."""
    if hard and totals['committed'] + bound > cap:return False
    return not research or totals['settled'] < research_limit(cap)


def forecast(cap, totals, *, hard=False):
    return dict(policy='committed-hard-cap' if hard else 'settled-research-1.25-closing-uncapped',
                cap_microusd=cap,settled_microusd=totals['settled'],provisional_microusd=totals.get('provisional',0),unresolved_liability_microusd=totals['liability'],
                active_reservation_microusd=totals['reservation'],committed_microusd=totals['committed'],
                research_stop_settled_microusd=research_limit(cap),
                admission_price_bound_available_microusd=max(0,cap-totals['committed']) if hard else None,
                closing_money_can_refuse=hard,forecast_excludes_routes_deadlines_capabilities=True)
