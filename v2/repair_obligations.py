"""Durable repair identity; ownership is not evidence that a repair is happening."""
from .global_planner import current, hashed


def identity(claim):
    objections=sorted((str(w),v,(claim.get('verdict_records') or {}).get(w))
                      for w,v in claim.get('verdicts',{}).items() if v in ('object','reject'))
    return hashed([claim['key'],current(claim),claim.get('revision',0),claim.get('post_type','reasoning'),objections])


def due(claim, now, owner=None, wait=300):
    transitions=[t for t in claim.get('transitions',[]) if t.get('to')=='needs-repair']
    since=(transitions[-1].get('at') if transitions else claim.get('at')) or 0
    return claim['state']=='needs-repair' and (owner is None or now-since>=wait)


def brief(claim):
    kind=claim.get('post_type','reasoning');rid=current(claim)
    return ('REPAIR the current %s claim #%s. Read its complete board text, continuations/artifacts and objection records %s. '
            'Address each objection, retaining the original scope or explicitly narrowing it. Post the revision with '
            'Post-type: %s and Revision-of: #%s. Preserve required evidence/dependency fields and source receipts. '
            'If unresolved, post a note explaining the blocker; do not claim acceptance.\n\nCURRENT FULL TEXT:\n%s' %
            (kind,rid,claim.get('verdict_records',{}),kind,rid,claim.get('text') or claim.get('statement','')))
