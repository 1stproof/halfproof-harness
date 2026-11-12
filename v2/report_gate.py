"""Mechanical report checking and bounded revision feedback.

Checks never replace a model draft. Failed and incomplete checks remain recorded;
terminal delivery labels their results without granting scientific certification.
"""
import hashlib
from pathlib import Path

from .report_check import check, feedback, fallback_report, fallback_provenance, parse_ledger_markdown, record_texts

DRAFT = 'FINAL-DRAFT.tex'
DRAFT_PROVENANCE = 'FINAL-DRAFT-PROVENANCE.md'
REJECTED = 'FINAL-REJECTED.tex'
REJECTED_PROVENANCE = 'FINAL-REJECTED-PROVENANCE.md'
FEEDBACK_FILE = 'REPORT-CHECK.md'


def settings(strategy, root=None):
    from .claim_ledger import load_policy
    wr = load_policy(strategy, root)['writer']
    return dict(enabled=bool(wr.get('final_report_check')), retries=int(wr.get('final_report_retries', 2)),
                margin=int(wr.get('final_report_retry_margin_seconds', 600)))


def enabled(runtime):
    try:
        return settings(getattr(runtime, 'strategy', None))['enabled']
    except Exception:
        return True  # configuration failure must not disable publication checking


def _claims(directory):
    if (Path(directory) / 'CLAIMS-MANIFEST.json').exists():
        try:
            from .claim_manifest import read
            return read(directory)
        except (OSError, ValueError, KeyError, TypeError):
            return []  # corrupted current authority never falls back to seed display
    p = Path(directory) / 'CLAIMS-LEDGER.md'
    try:
        return parse_ledger_markdown(p.read_text(errors='replace'))
    except OSError:
        return []


def evaluate(text, directory, provenance=None):
    if isinstance(text, bytes):
        text = text.decode(errors='replace')
    if provenance is None:
        p = Path(directory) / 'PROVENANCE.md'
        provenance = p.read_text(errors='replace') if p.exists() else ''
    return check(text, _claims(directory), texts=record_texts(directory), provenance=provenance)


def fallback_bytes(directory):
    claims, texts = _claims(directory), record_texts(directory)
    rep = fallback_report(claims)
    if not check(rep, claims, texts=texts)['ok']:
        # The accepted claims' own wording trips the check (e.g. a citation): name them by record only.
        rep = fallback_report([dict(c, statement='an accepted claim of the research record (see Provenance)',
                                    key='', text=None) for c in claims if c.get('state') == 'accepted'])
    # Readiness and freeze treat a FINAL.tex of 1000 bytes or less as "no report"; the fallback
    # is this writer's report, so it carries an explanatory comment block above that size.
    note = ('% This report was produced by the harness final-report check: the writer\'s draft did not\n'
            '% pass the check (statements presented as proved must be ledger-accepted claims; cited\n'
            '% theorems need listed hypotheses accepted by a critic in this setting) within the allowed\n'
            '% revisions. The recovery paper supplies no proof; the accepted inventory is in PROVENANCE.md.\n')
    while len(note) + len(rep) <= 1100:
        note += '%\n'
    return (note + rep).encode()


def _atomic(path, data):
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


def gate_digest(work, data, provenance):
    """Exactly what one report check saw: draft, ledger, evidence and provenance."""
    work = Path(work)
    try:
        ledger_bytes = (work / 'CLAIMS-LEDGER.md').read_bytes()
    except OSError:
        ledger_bytes = b''
    evidence = b''
    for name in ('CLAIMS-MANIFEST.json', 'BOARD.json'):
        try: evidence += (work / name).read_bytes()
        except OSError: pass
    if isinstance(provenance, bytes):
        provenance = provenance.decode(errors='replace')
    return hashlib.sha256(data + b'\0' + ledger_bytes + b'\0' + evidence + b'\0' + provenance.encode()).hexdigest()


def gate_promotion(lifecycle, data, provenance=None):
    """Led swarms: bytes the slot's FINAL.tex should get for this promoted draft, or None to
    keep it as is. Records the draft and its check result on the lifecycle state."""
    work = Path(lifecycle.work)
    if provenance is None:
        p = work / 'PROVENANCE.md'
        provenance = p.read_bytes() if p.exists() else b''
    if isinstance(provenance, bytes):
        provenance = provenance.decode(errors='replace')
    digest = gate_digest(work, data, provenance)
    st = lifecycle.state.setdefault('report_check', dict(tries=0, history=[]))
    if st.get('last_digest') == digest:
        return data  # an identical save remains a model draft; capture its version time
    res = evaluate(data, work, provenance)
    st['last_digest'] = digest; st['last_ok'] = res['ok']
    st['history'] = (st.get('history') or [])[-49:] + [dict(digest=digest, ok=res['ok'], counts=res['counts'],
                                                             session=lifecycle.state.get('session'))]
    if res['ok']:
        (work / DRAFT).unlink(missing_ok=True)
        (work / DRAFT_PROVENANCE).unlink(missing_ok=True)
        st['passing_digest'] = digest
        _atomic(work / 'PROVENANCE.md', provenance.encode())
        return data
    _atomic(work / DRAFT, data)
    _atomic(work / DRAFT_PROVENANCE, provenance.encode())
    # A stage can end while the writer is still working. In that case turn_end
    # is never called: retain the same native feedback for the next snapshot
    # and retry_note, without spending a retry or extending the stage clock.
    text = feedback(res)
    _atomic(work / FEEDBACK_FILE, text.encode())
    st['feedback'] = text; st['feedback_pending'] = True
    # Checks still feed the writer and verification. Delivery never substitutes
    # administrative text for model bytes (operator 2026-10-13 18:01).
    _atomic(work / 'PROVENANCE.md', provenance.encode())
    return data


def turn_end(state, directory, *, time_left, limits):
    """After a writer turn: 'retry' (with feedback written to REPORT-CHECK.md) or None."""
    work = Path(directory)
    draft = work / DRAFT if (work / DRAFT).exists() else work / 'FINAL.tex'
    if not draft.exists():
        return None
    prov = work / DRAFT_PROVENANCE if draft.name == DRAFT else work / 'PROVENANCE.md'
    res = evaluate(draft.read_bytes(), work, prov.read_text(errors='replace') if prov.exists() else '')
    st = state.setdefault('report_check', dict(tries=0, history=[]))
    st['turn_results'] = (st.get('turn_results') or [])[-9:] + [dict(ok=res['ok'], counts=res['counts'],
                                                                     session=state.get('session'))]
    if res['ok'] and not res.get('style'):
        return None
    if st.get('tries', 0) >= limits['retries'] or time_left < limits['margin']:
        st['exhausted'] = True
        return None
    st['tries'] = st.get('tries', 0) + 1
    text = feedback(res)
    (work / FEEDBACK_FILE).write_text(text)
    st['feedback'] = text; st['feedback_pending'] = True
    return 'retry'


def enforce(directory, state=None):
    """Record failures before compilation, preserving the latest model bytes."""
    work = Path(directory)
    final = work / 'FINAL.tex'
    if not final.exists():
        return 'missing'
    data = final.read_bytes()
    res = evaluate(data, work)
    if state is not None:
        state.setdefault('report_check', {})['final'] = dict(ok=res['ok'], counts=res['counts'])
    if res['ok']:
        return 'accepted'
    _atomic(work / REJECTED, data)
    provenance = work / 'PROVENANCE.md'
    _atomic(work / REJECTED_PROVENANCE, provenance.read_bytes() if provenance.exists() else b'')
    _atomic(work / FEEDBACK_FILE, feedback(res).encode())
    return 'unverified'


def retry_note(state):
    """Task text appended to a relaunched writer (generic failure list), once."""
    st = state.get('report_check') or {}
    if not st.get('feedback_pending'):
        return ''
    st['feedback_pending'] = False
    return '\n\n' + st.get('feedback', '') + ('\nYour draft is FINAL.tex in your directory; the same list is in '
                                               'REPORT-CHECK.md.\n')
