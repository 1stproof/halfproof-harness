"""Proposer controls (redesign items 2a/2b). Parameters live in roles/<swarm>/policy.json
under "proposer" (see claim_ledger.DEFAULT_PROPOSER). Nothing here rejects a post.

2a  self_check: proposer-role agents get SELF_CHECK_LINE in their brief.
2b  max_unreviewed_claims: at a request boundary, a proposer with that many of its own
    claims still open gets NUDGE. A transport retry of the identical request gets the
    identical message (same hash discipline as refresh_board).
"""
from .claim_ledger import load_policy, DEFAULT_PROPOSER

SELF_CHECK_LINE = ("Before posting a claim, check it yourself: re-derive it, test a special case, and look for a "
                   "counterexample; post the check with the claim under a line 'Self-check:'.")
NUDGE = ("You have {n} claims awaiting review. Before proposing new ones, check, critique or extend existing "
         "claims (see the claim list).")


def _proposer_param(policy, key):
    return (policy.get('proposer') or {}).get(key, DEFAULT_PROPOSER[key])


def is_proposer(policy, model, worker=None, role=None):
    """A roster role decides when known (ideas proposes; critic and literature do not).
    Otherwise: not a critic by policy, or a model the roster lists as an ideas slot."""
    if role:
        return role == 'ideas'
    from .board import MODEL_KEYS
    r = policy.get('roster')
    if r is None and policy.get('_swarm'):
        from .roles import roster as _roster
        r = _roster(policy['_swarm'])
    ideas = [str(m).lower() for m in ((r or {}).get('ideas') or [])]
    if str(model).lower() in ideas:
        return True
    crit = {str(m).lower() for m in policy.get('critic_models') or ()}
    name = MODEL_KEYS.get(str(model).lower(), str(model))
    return str(name).lower() not in crit and str(model).lower() not in crit and (worker is None or worker not in set(policy.get('critic_workers') or ()))


def self_check_line(swarm, model, root=None, role=None):
    """'' or the self-check brief line for this model (or roster role) in this swarm."""
    pol = dict(load_policy(swarm, root), _swarm=swarm)
    if not _proposer_param(pol, 'self_check') or not is_proposer(pol, model, role=role):
        return ''
    return SELF_CHECK_LINE


def unreviewed(claims, worker):
    return sorted(k for k, c in claims.items() if c.get('proposer') == worker and c.get('state') == 'open')


def nudge(runtime, w, request):
    """Called at the top of SwarmRuntime.refresh_board. Appends NUDGE to request when due."""
    from .swarm_executor import hashed
    if getattr(w, 'stage', None) != 'research' or getattr(getattr(w, 'context', None), 'role', 'research') != 'research':
        return
    state = w.state.setdefault('proposer_nudge', {})
    h = hashed(request)
    if state.get('request_hash') == h:
        # A transport retry repeats the recorded decision, including "nothing".
        if state.get('message'):
            request.setdefault('messages', []).append(dict(role='user', content=state['message']))
        return
    msg = ''
    try:
        pol = dict(load_policy(getattr(runtime, 'strategy', None)), _swarm=getattr(runtime, 'strategy', None))
        limit = _proposer_param(pol, 'max_unreviewed_claims')
        # Short harness key (dsf, k3, ...), not the provider id in w.context.model.
        model = ((getattr(runtime, 'workers', None) or {}).get(w.slot_id) or getattr(getattr(w, 'run', None), 'lane', None)
                 or getattr(getattr(w, 'context', None), 'model', ''))
        role = w.state.get('member_role')
        if role is None:
            from .open_swarm import member_role
            role = member_role(w.slot_id)
        if limit and is_proposer(pol, model, w.slot_id, role=role) and runtime.role_allowed(w):
            now = runtime.clock()
            cache = runtime.__dict__.setdefault('_proposer_ledger', {})
            if 'claims' not in cache or now - cache.get('at', -1e18) >= _proposer_param(pol, 'nudge_check_seconds'):
                cache.update(claims=runtime.claim_ledger(pol), at=now)
            waiting = unreviewed(cache['claims'], w.slot_id)
            if len(waiting) >= limit and waiting != state.get('waiting'):  # once per change in the waiting set
                msg = NUDGE.format(n=len(waiting))
                state['nudges'] = state.get('nudges', 0) + 1
            state['waiting'] = waiting
    except Exception:
        msg = ''  # a control must never break a request
    if msg:
        request.setdefault('messages', []).append(dict(role='user', content=msg))
    state.update(request_hash=h, message=msg)
    w.save()

def _seed_under_policy(text, pol):
    """Review 2026-10-08: a ledger table written before the writer's two-model rule cannot show which
    models accepted a claim, so under that rule its accepted rows are read as awaiting-audit. Each
    table is judged by the rule mark (claim_ledger.LEDGER_RULE_MARK) written just before it; rows are
    recognised as the final-report check reads them (stripped lines, cells split on unescaped pipes)."""
    import re
    from .claim_ledger import min_accept_models, LEDGER_RULE_RE
    try:
        need = min_accept_models(pol)
    except Exception:
        need = 1
    if need <= 1 or not text:
        return text
    out, mark, table, changed = [], None, None, 0
    for line in str(text).split('\n'):
        st = line.strip()
        m = LEDGER_RULE_RE.match(st)
        if m:
            mark = int(m.group(1)); out.append(line); continue
        parts = re.split(r"(?<!\\)\|", st)  # exactly as report_check.parse_ledger_markdown splits a row
        cells = [c.strip() for c in parts[1:-1]]
        state_col = 2 if len(cells) == 7 else 1
        if len(cells) in (6, 7) and cells[state_col] == 'State':
            table, mark = mark, None  # this table's rule: the mark written before it, if any
        elif len(cells) in (6, 7) and cells[state_col] == 'accepted' and (table is None or table < need):
            parts[state_col + 1] = ' awaiting-audit '; changed += 1
            line = line[:len(line) - len(line.lstrip())] + '|'.join(parts)
        out.append(line)
    text = '\n'.join(out)
    if changed:
        text += ('\nNote: %d accepted rows of earlier tables predate the rule that a verified claim needs accepts from '
                 'critics of at least %d different models; they are listed as awaiting-audit.\n' % (changed, need))
    return text


def write_claims_ledger(runtime, directory):
    """CLAIMS-LEDGER.md into a writer's directory (items 4/6). Never raises."""
    from pathlib import Path
    from .claim_ledger import ledger_markdown
    pol = None
    try:
        pol = load_policy(getattr(runtime, 'strategy', None))
        claims = runtime.claim_ledger(pol)
        text = ledger_markdown(claims, pol)
        from .claim_manifest import manifest
        data = manifest(claims, pol, str(getattr(runtime, 'claim_namespace', getattr(runtime, 'path', Path(directory).resolve()))))
        (Path(directory) / 'CLAIMS-MANIFEST.json').write_text(__import__('json').dumps(data, sort_keys=True))
    except Exception as exc:
        data = None
        text = '# Claims ledger\n\nThe ledger could not be built (%s); treat every board claim as unverified.\n' % type(exc).__name__
    if data is not None:
        # The per-claim index is a reading aid: its failure never demotes the ledger.
        try:
            write_claim_evidence(data, directory)
        except Exception as exc:
            text += ('\nCLAIMS-INDEX.json could not be built (%s); read CLAIMS-MANIFEST.json directly.\n'
                     % type(exc).__name__)
    try:
        prior = bool(getattr(runtime, 'closing_only', lambda: False)()) or bool(getattr(runtime, 'seed_files', None))
    except Exception:
        prior = False
    seed_copy = Path(directory) / 'CLAIMS-LEDGER-SEED.md'
    live = Path(directory) / 'CLAIMS-LEDGER.md'
    MARK = '.claims-ledger-generated'
    if prior and not seed_copy.exists() and live.exists():
        try:
            if not (Path(directory) / MARK).exists():seed_copy.write_text(live.read_text())  # the seed's ledger, kept once before the first rewrite
        except OSError:
            pass
    if prior:
        # Render only current authority, independent of prior generated files.
        # Keep the original seed separately for archival evidence, never read or
        # nest it in the writer's current ledger.
        text += ('\nCLAIMS-MANIFEST.json is the current authority. Historical seed rows '
                 'confer no acceptance; claims recorded only in PRIOR-SUMMARY.md or '
                 'PRIOR-WORK-NNN.md are unverified.\n')
    try:
        (Path(directory) / 'CLAIMS-LEDGER.md').write_text(text)
        (Path(directory) / MARK).write_text('')
    except OSError:
        pass
    return text


def write_claim_evidence(data, directory):
    """Give every current manifest row a direct path, preserving its full content."""
    import json
    from pathlib import Path
    root = Path(directory) / 'claim-evidence'
    root.mkdir(exist_ok=True)
    current = {claim['claim_id'] + '.json' for claim in data['claims']}
    for stale in root.glob('*.json'):
        if stale.name not in current:stale.unlink()  # superseded claims never sit beside current ones
    rows = []
    for claim in data['claims']:
        identity = claim['claim_id']
        if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('unsafe claim evidence identity')
        path = 'claim-evidence/' + identity + '.json'
        (Path(directory) / path).write_text(json.dumps(claim, ensure_ascii=False, sort_keys=True))
        rows.append(dict(claim_id=identity, current_record=claim['current_record'],
                         state=claim['state'], post_type=claim.get('post_type'),
                         title=str(claim.get('statement') or '')[:200], path=path))
    (Path(directory) / 'CLAIMS-INDEX.json').write_text(json.dumps(dict(
        authority='CLAIMS-MANIFEST.json', claims=rows), ensure_ascii=False, indent=1))
    # Operator 2026-10-14: the writer receives every certified proof inline, not behind paged reads.
    from .proof_assembly import certified_proofs_md
    (Path(directory) / 'CERTIFIED-PROOFS.md').write_text(certified_proofs_md(data))
