"""Claim ledger (redesign item 1): the acceptance state of every claim, computed from
board records. Pure: no I/O except load_policy, nothing persisted beyond the board.

Records are board records as in Board.export_snapshot()['records'] — dicts with
id, kind, author, body — optionally carrying 'model' (otherwise looked up in the
`models` map worker -> model). Malformed records are skipped, never raised on.

A claim is a 'finding' or 'claim' record that is not a verdict. A verdict is a
finding/comment whose Status is accept/reject/retract, posted by a critic other than
the claim's proposer, matching the claim by task_key or by "Re: <record id>".
Each critic's LATEST verdict on a claim counts (a retraction removes it).
"""
import json
import re
from pathlib import Path

DEFAULT_ACCEPTANCE = {"k_accept": 2, "k_refute": 1, "n_critics": 3}  # operator 2026-10-06: one rejection refutes; two accepts to accept
# Redesign items 2a/2b and 4/6. writer.min_critic_accepts None = acceptance.k_accept.
DEFAULT_PROPOSER = {"self_check": True, "max_unreviewed_claims": 1, "nudge_check_seconds": 30}
DEFAULT_WRITER = {"min_critic_accepts": None, "final_report_check": False, "final_report_retries": 2,
                  "final_report_retry_margin_seconds": 600,  # final-report check (v2/report_gate.py), off unless a run turns it on
                  "min_accept_models": 1}
# Operator 2026-10-08 (p01 open S1 #21: a wrong claim accepted by two critic slots of one model and
# written up as proved): writer.min_accept_models = how many distinct models must be among a claim's
# counting accepts before the writer may use it as verified. A claim accepted by slots of fewer
# models stays accepted (review moves on) but is routed to an auditor of another model
# (audit.models); the audit verdict counts like any critic verdict (a reject refutes, an
# objection reopens it as needs-repair, an accept adds the second model).
DEFAULT_AUDIT = {"models": []}
MODEL_FAMILY = {"dsf": "DSF", "dsp": "DSP", "qwen24t": "Qwen", "k3": "K3", "glm": "GLM"}  # harness model keys -> board families
DEFAULT_CRITIC_MODELS = {"open-swarm": ["K3", "Qwen", "GLM"], "mixed-swarm": ["K3", "Qwen"], "dsf-swarm": []}
ROOT = Path(__file__).resolve().parent.parent / 'roles'

ACCEPT = {'accept', 'accepted', 'verified', 'confirmed', 'proved', 'proven', 'correct', 'approve', 'approved'}
REJECT = {'reject', 'rejected', 'refuted', 'refute', 'disproved', 'false', 'wrong', 'incorrect'}
# Operator 2026-10-07: an objection short of an explicit reject (sourcing, gaps, repairs) marks the
# claim needs-repair and sends it back to its proposer; only an explicit reject refutes.
OBJECT = {'needs', 'need', 'objection', 'objections', 'object', 'objects', 'unsourced', 'incomplete', 'revise', 'revision',
          'repair', 'gap', 'gaps', 'unclear', 'insufficient', 'concern', 'concerns', 'fix', 'question', 'questionable', 'doubt'}
RETRACT = {'retract', 'retracted', 'retraction', 'withdraw', 'withdrawn', 'withdraws'}
CRITIC_ROLES = {'critic', 'reviewer', 'referee', 'checker'}


def default_policy(swarm=None):
    return {"acceptance": dict(DEFAULT_ACCEPTANCE),
            "critic_models": list(DEFAULT_CRITIC_MODELS.get(swarm, ["K3", "Qwen"])),
            "critic_workers": [],
            "proposer": dict(DEFAULT_PROPOSER), "writer": dict(DEFAULT_WRITER), "audit": dict(DEFAULT_AUDIT)}


STANDING_SESSION = re.compile(r'((?:critic|open):[A-Za-z0-9_.-]+):\d+(?::(?:ideas|critic|literature))?')  # role-mix sessions carry their role


def _voter(worker):
    """Voting identity of a worker: sessions 'critic:<slot>:<n>' of a standing critic slot, and 'open:<slot>:<n>' of an open
    member slot, share one."""
    m = STANDING_SESSION.fullmatch(str(worker))
    return m.group(1) if m else worker


def policy_overrides():
    """V3_POLICY_OVERRIDES: a JSON object deep-merged over every swarm's policy.json for this
    run (e.g. {"proposer": {"max_unreviewed_claims": 2}}); absent or not an object = {}."""
    import os
    try:
        v = json.loads(os.environ.get('V3_POLICY_OVERRIDES') or '{}')
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def parse_policy_overrides(text):
    """--policy-overrides value: a JSON object, else ValueError. Unknown keys are kept and
    ignored by load_policy."""
    v = json.loads(text)
    if not isinstance(v, dict):
        raise ValueError('policy overrides must be a JSON object')
    return v


def _deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_policy(swarm, root=None):
    """roles/<swarm>/policy.json merged over the defaults; absent/broken file = defaults."""
    pol = default_policy(swarm)
    try:
        data = json.loads(((Path(root) if root else ROOT) / str(swarm) / 'policy.json').read_text() or '{}')
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data = _deep_merge(data, policy_overrides())
    acc = data.get('acceptance')
    if isinstance(acc, dict):
        for k in DEFAULT_ACCEPTANCE:
            if isinstance(acc.get(k), int) and acc[k] > 0:
                pol['acceptance'][k] = acc[k]
        if isinstance(acc.get('distinct_from_proposer_model'), bool):
            pol['acceptance']['distinct_from_proposer_model'] = acc['distinct_from_proposer_model']
        if acc.get('mode') == 'pair':
            pol['acceptance']['mode'] = 'pair'
            pm = acc.get('pair_models')
            pol['acceptance']['pair_models'] = [str(m).lower() for m in pm] if isinstance(pm, list) and len(pm) == 2 else ['dsf', 'k3']
            tb = acc.get('tiebreak') if isinstance(acc.get('tiebreak'), dict) else {}
            pol['acceptance']['tiebreak'] = dict(model=str(tb.get('model') or 'k3').lower(),
                                                 show_critiques=tb.get('show_critiques', True) is not False)
    prop = data.get('proposer')
    if isinstance(prop, dict):
        if isinstance(prop.get('self_check'), bool):
            pol['proposer']['self_check'] = prop['self_check']
        for k in ('max_unreviewed_claims', 'nudge_check_seconds'):
            if isinstance(prop.get(k), int) and not isinstance(prop.get(k), bool) and prop[k] >= 0:
                pol['proposer'][k] = prop[k]
    wr = data.get('writer')
    if isinstance(wr, dict) and isinstance(wr.get('min_critic_accepts'), int) and not isinstance(wr['min_critic_accepts'], bool) \
            and wr['min_critic_accepts'] > 0:
        pol['writer']['min_critic_accepts'] = wr['min_critic_accepts']
    if isinstance(wr, dict) and isinstance(wr.get('final_report_check'), bool):
        pol['writer']['final_report_check'] = wr['final_report_check']
    for k in ('final_report_retries', 'final_report_retry_margin_seconds'):
        if isinstance(wr, dict) and isinstance(wr.get(k), int) and not isinstance(wr[k], bool) and wr[k] >= 0:
            pol['writer'][k] = wr[k]
    if isinstance(wr, dict) and isinstance(wr.get('min_accept_models'), int) and not isinstance(wr['min_accept_models'], bool) \
            and wr['min_accept_models'] > 0:
        pol['writer']['min_accept_models'] = wr['min_accept_models']
    ce = data.get('certification')
    if isinstance(ce, dict):
        pol['certification'] = dict(enabled=ce.get('enabled') is True, checker=str(ce.get('checker') or 'qwen24t').lower(),
                                    interval_seconds=ce['interval_seconds'] if isinstance(ce.get('interval_seconds'), int) and ce['interval_seconds'] > 0 else 7200,
                                    final_before_stop_seconds=ce['final_before_stop_seconds'] if isinstance(ce.get('final_before_stop_seconds'), int) and ce['final_before_stop_seconds'] >= 0 else 3600)
        strict = ce.get('strict_checking', False)
        if type(strict) is not bool:
            raise ValueError('boolean required: strict_checking')
        pol['certification']['strict_checking'] = strict
        count = ce.get('required_certifiers')
        if count is not None and (type(count) is not int or count not in (2, 3)):
            raise ValueError('required_certifiers must be 2, 3 or null (legacy)')
        pol['certification']['required_certifiers'] = count
        for key, default in (('initial_round_after_seconds',60), ('selector_session_seconds',3600), ('selector_retry_initial_seconds',60), ('selector_retry_max_seconds',900)):
            value=ce.get(key, default)
            if type(value) is not int or value <= 0:raise ValueError('positive integer required: '+key)
            pol['certification'][key]=value
        if 'all_pair_accepted' in ce:
            if type(ce['all_pair_accepted']) is not bool:raise ValueError('boolean required: all_pair_accepted')
            pol['certification']['all_pair_accepted']=ce['all_pair_accepted']
    au = data.get('audit')
    if isinstance(au, dict) and isinstance(au.get('models'), list):
        pol['audit']['models'] = [str(x) for x in au['models']]
    for k in ('critic_models', 'critic_workers'):
        if isinstance(data.get(k), list):
            pol[k] = [str(x) for x in data[k]]
    cw = data.get('critic_weights')
    if isinstance(cw, dict):
        pol['critic_weights'] = {str(m): str(v).lower() for m, v in cw.items() if str(v).lower() in ('full', 'advisory', 'block', 'none')}
    return pol


def legacy_verdict(status):
    """Only historical verdict words confer implicit review intent."""
    word = re.findall(r"[a-z][a-z-]*", str(status or "").lower())
    return bool(word and word[0] in {"accept", "reject", "objection"})


def verdict_word(status):
    """'accept' | 'reject' | 'object' | 'retract' | None from a free-form status (first word)."""
    words = re.findall(r"[a-z][a-z-]*", str(status or '').lower().replace('_', '-'))
    if not words:
        return None
    w = words[0]
    return 'accept' if w in ACCEPT else 'reject' if w in REJECT else 'retract' if w in RETRACT else 'object' if w in OBJECT or w.split('-')[0] in OBJECT else None


def _key(text):
    text = ' '.join(str(text).lower().split())
    return re.sub(r'^[*_`~\s]+|[*_`~\s]+$', '', text)[:300]


def _fields(text):
    out = {}
    for line in str(text or '').splitlines()[:8]:
        m = re.match(r"\s*[*#>_-]*\s*[*_]*\s*(status|task|claim|re|role|post-type|revision-of)\s*[*_]*\s*[:=]\s*(.*)$", line, re.I)
        if m and m.group(2).strip():
            out.setdefault(m.group(1).lower(), re.sub(r'^[\s*_`]+|[\s*_`]+$', '', m.group(2)))
    return out


# ---- duplicate claims (operator 2026-10-07) ----
# A post that restates an existing claim does not create a new claim. A declared repair /
# re-registration of claim #N, or near-identical statement text, attaches to claim #N: with new
# content it becomes a new revision (supersedes the text, resets the verdicts); with no new
# content it is a restatement (the board does not post it and tells the proposer). A restatement
# of a refuted claim is a restatement unless it explicitly addresses the refutation. When unsure,
# the post is a new claim: nothing here rejects on format.
POINTER = re.compile(r"\b(?:repair\w*|re-?registration|re-?register\w*|restat\w*|re-?post\w*|re-?submi\w*|resubmission"
                     r"|revision|revised\s+version|supersed\w*)\s+(?:of|for|to|on)\s+(?:the\s+)?(?:refuted\s+|earlier\s+|my\s+)?"
                     r"(?:claim\s+|record\s+|board\s+record\s+|finding\s+)?(?:#|board\s*:\s*|no\.\s*)?\s*(\d+)\b", re.I)
ADDRESSES = re.compile(r"\b(?:answer\w*|address\w*|fix\w*|rebut\w*|resolv\w*|repair\w*|avoid\w*|handl\w*)\s+(?:the\s+|this\s+|that\s+|its\s+)?"
                       r"(?:refutation|rejection|counter-?example|objection)s?\b"
                       r"|\b(?:refutation|rejection|counter-?example)s?\s+(?:in|of|at|from|by)\s+(?:#|board\s*:|record\s+)", re.I)
_LABELS = re.compile(r"^\s*[*#>_-]*\s*[*_]*\s*(?:status|task|claim|re|role|self[ _-]?check|evidence|verified[ _-]?by)\s*[*_]*\s*[:=]", re.I)
TITLE_JACCARD = 0.85   # same statement: word sets of the statement lines this close
TITLE_MIN_WORDS = 6    # shorter statements are too generic to call duplicates
SAME_TEXT = 0.9        # no new content: word 3-gram sets of the full texts this close


def _words(text):
    import unicodedata
    t = unicodedata.normalize('NFKD', str(text or '')).lower()
    t = t.replace('\u2212', '-')  # Unicode minus is a sign
    return re.findall(r"[a-z0-9\u0370-\u03ff]+|[<>=+*/^\u2264\u2265\u2260\u2282\u2286\u228a\u2208\u2209]|-(?=\s*[0-9a-z(\u0370-\u03ff])", t)  # symbols and signs are content


def _title(p):
    """The statement line(s): Task/Claim value or first line, with repair pointers removed."""
    t = p.get('task_line') or p.get('first') or ''
    t = re.sub(r"\((?:[^()]*\b(?:repair|re-?registration|restat|revis|supersed|per objection)[^()]*)\)", ' ', t, flags=re.I)
    return [w for w in _words(t) if w not in ('repair', 'of', 'per', 'objection', 'the', 'a', 'an')]


_ADMIN = re.compile(r"^\s*[*#>_-]*\s*[*_]*\s*(?:status|re|role)\s*[*_]*\s*[:=].*$", re.I)
NEGATIONS = {'not', 'no', 'never', 'none', 'false', 'fails', 'fail', 'cannot', 'nonexistence', 'non', 'neither', 'nor',
             'without', 'counterexample', 'disproved', 'refuted', 'isn', 'doesn', 'don', 't'}


def _grams(text, n=3):
    # Administrative lines (Status/Re/Role) are ignored; Task/Claim/Evidence values are content.
    w = [x for x in _words('\n'.join(_LABELS.sub(' ', l) for l in str(text or '').splitlines() if not _ADMIN.match(l)))]
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))} or {tuple(w)}


def _jac(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def same_title(a, b):
    """Same statement: word sequences this close, and the same negations (an opposite statement is new)."""
    import difflib
    if len(a) < TITLE_MIN_WORDS or len(b) < TITLE_MIN_WORDS or set(a) & NEGATIONS != set(b) & NEGATIONS:
        return False
    marks = lambda x: [m for w in x for m in (re.findall(r"\d+", w) if w.isalnum() else [w])]  # numbers (also inside tokens) and symbols, in order
    if marks(a) != marks(b) or _jac(a, b) < 0.7:
        return False  # different numbers (Task 1 / Task 2, case n = 3 / n = 4) are different statements
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= TITLE_JACCARD


def _content(text):
    t = _pointer(text)[1]
    return [x for x in _words('\n'.join(_LABELS.sub(' ', l) for l in str(t or '').splitlines() if not _ADMIN.match(l)))]


def same_content(a, b):
    """No new content: the same words and symbols in the same order, ignoring case, punctuation,
    Status/Re/Role lines and a repair pointer (any addition or correction is new content)."""
    return _content(a) == _content(b)


def _pointer(text):
    """(record number, text without the pointer phrase) of a declared repair/restatement, else (None, text)."""
    head = '\n'.join(str(text or '').splitlines()[:12])[:1500]
    m = POINTER.search(head)
    return (int(m.group(1)), str(text or '').replace(m.group(0), ' ', 1)) if m else (None, str(text or ''))


CLAIM_LINE = re.compile(r"^\s*[*#>_-]*\s*[*_]*\s*claim\s*[*_]*\s*[:=]\s*\S", re.I | re.M)
# Roles whose non-verdict posts are notes, not claims, unless they carry an explicit "Claim:" line.
NON_PROPOSER_ROLES = CRITIC_ROLES | {'literature'}

SELF_CHECK = re.compile(r"^\s*[*#>_-]*\s*[*_]*\s*self[ _-]?check\s*[*_]*\s*:", re.I | re.M)


def _state(verdicts, acc):
    """Model-agnostic state (any standing reject refutes); the ledger adds the proposer-model rule."""
    a = sum(v == 'accept' for v in verdicts.values())
    r = sum(v == 'reject' for v in verdicts.values())
    if r < acc['k_refute'] and any(v == 'object' for v in verdicts.values()):
        return 'needs-repair'
    return 'refuted' if r >= acc['k_refute'] else 'accepted' if a >= acc['k_accept'] else 'disputed' if a and r else 'open'


def _ref(value):
    m = re.search(r"\d+", str(value or ''))
    return int(m.group()) if m else None


def _parse(rec, models):
    """Normalized view of one record, or None when it cannot be one of ours."""
    if not isinstance(rec, dict) or rec.get('kind') not in ('finding', 'claim', 'comment'):
        return None
    body = rec.get('body')
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {'text': body}
    if not isinstance(body, dict):
        return None
    worker = rec.get('author')
    if not isinstance(worker, str) or not worker:
        return None
    text = body.get('statement') or body.get('text') or ''
    f = _fields(text)
    declared_type = (None if body.get('implicit_type') and body.get('post_type') == 'reasoning' else body.get('post_type')) or ('reasoning' if body.get('revision_of') is not None else None)
    status = body.get('raw_status') or body.get('status') or f.get('status')
    if rec['kind'] == 'comment' and not f.get('status'):
        status = None
    task = body.get('task') or f.get('task') or f.get('claim')
    key = body.get('task_key') or (_key(task) if task else None)
    return dict(id=rec.get('id'), kind=rec['kind'], worker=worker, at=rec.get('created'),
                model=rec.get('model') or body.get('model') or (models or {}).get(worker),
                role=str(body.get('role') or f.get('role') or '').lower().split(' ')[0] or None,
                key=key, re=_ref(body.get('re') or f.get('re') or (body.get('about') if rec['kind'] == 'comment' else None)),
                explicit_verdict=declared_type == 'verdict' and not body.get('implicit_type'),
                verdict=verdict_word(status) if status and (declared_type == 'verdict' and not body.get('implicit_type') or legacy_verdict(status)) and worker != 'controller' and declared_type not in ('note', 'reasoning', 'literature') else None,  # the controller never judges
                repair_depth=body.get('repair_depth') or 0, repair_author_families=body.get('repair_author_families') or [], repair_alternative=body.get('repair_alternative'), repair_of=body.get('repair_of'), repair_verdict=body.get('repair_verdict'), repair_complete=body.get('repair_complete'),
                literature=body.get('literature'), premise_refs=body.get('premise_refs',[]), literature_evidence_required=body.get('literature_evidence_required',False), literature_evidence_verified=body.get('literature_evidence_verified',False), literature_refs=body.get('literature_refs', []), post_type=declared_type, revision_of=_ref(body.get('revision_of')),
                explicit_claim=bool(body.get('claim')) or any(isinstance(v, str) and CLAIM_LINE.search(v) for v in body.values()),
                text='\n'.join(str(body.get(k) or '') for k in ('statement', 'text', 'plan', 'evidence') if isinstance(body.get(k), str))
                     + ('\nLiterature: ' + json.dumps(body['literature'], sort_keys=True) if isinstance(body.get('literature'), dict) else ''),
                first=next((l.strip('# ').strip() for l in str(body.get('task') or text).splitlines() if l.strip()), '')[:200],
                task_line=str(task or '')[:400], parts=body.get('parts') if isinstance(body.get('parts'), int) else 1,
                # Continuation import (v2/claim_import.py): only the controller can post one.
                imported=body.get('imported') if worker == 'controller' and isinstance(body.get('imported'), dict) else None)


CONTINUED = re.compile(r"^continued from record (\d{1,12}) \(part (\d{1,6}) of (\d{1,6})\)$")


def _body(rec):
    b = rec.get('body') if isinstance(rec, dict) else None
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError:
            return None
    return b if isinstance(b, dict) else None


def assemble(records):
    """{head record id: (field, full text)} for posts stored in several records: the board's long
    posts (the head, then comments 'continued from record R (part k of N)' by the same author) and
    the controller's imported claims (the head, then controller comments with 're' R and 'part' k).
    The board writes a post's parts in one transaction, so part k is record R + k - 1; only such
    complete, contiguous sets are assembled (a later comment can never stand in for a part).
    Parts are joined exactly as split (at any character)."""
    recs = {r['id']: r for r in records or () if isinstance(r, dict) and isinstance(r.get('id'), int)}
    out = {}
    for rid, r in recs.items():
        b = _body(r)
        if b is None:
            continue
        field = next((f for f in ('statement', 'plan', 'text') if isinstance(b.get(f), str)), None)
        nxt = recs.get(rid + 1)
        nb = _body(nxt) if nxt else None
        if field is None or nb is None or nxt.get('kind') != 'comment' or nxt.get('author') != r.get('author'):
            continue
        m = CONTINUED.match(str(nb.get('about') or ''))
        if m and int(m.group(1)) == rid and int(m.group(2)) == 2:
            n = int(m.group(3))
            def ok(k, x, xb):
                mm = CONTINUED.match(str(xb.get('about') or ''))
                return bool(mm) and (int(mm.group(1)), int(mm.group(2)), int(mm.group(3))) == (rid, k, n)
        elif r.get('author') == 'controller' and nb.get('re') == rid and nb.get('part') == 2 and isinstance(nb.get('of'), int) \
                and 2 <= nb['of'] <= 10 ** 6:
            n = nb['of']
            def ok(k, x, xb):
                return xb.get('re') == rid and xb.get('part') == k and xb.get('of') == n
        else:
            continue
        rest = []
        for k in range(2, n + 1):
            x = recs.get(rid + k - 1)
            xb = _body(x) if x else None
            if xb is None or x.get('kind') != 'comment' or x.get('author') != r.get('author') or not ok(k, x, xb) \
                    or not isinstance(xb.get('text'), str):
                break
            rest.append(xb['text'])
        else:
            out[rid] = (field, b[field] + ''.join(rest))
    return out


def ledger(records, policy=None, models=None):
    """claim_key -> {key, statement, proposer, proposer_model, records: {proposal, verdicts},
    verdicts: {critic_worker: 'accept'|'reject'}, state: open|accepted|refuted|disputed,
    accepts, rejects, self_checked, queued, route_after}.

    self_checked: the proposal (or its continuation parts) carries a 'Self-check:' line.
    queued: when posted, the proposer already had proposer.max_unreviewed_claims claims
    in state open; route_after lists those earlier claim keys (routing defers the claim
    until they leave 'open'). Nothing is ever rejected.

    records: iterable of board records (see module doc), in posting order (sorted by id).
    policy: as load_policy(); None = defaults. models: optional {worker: model}.
    """
    policy = policy or default_policy()
    acc = dict(DEFAULT_ACCEPTANCE, **(policy.get('acceptance') or {}))
    crit_models = {str(m).lower() for m in policy.get('critic_models') or ()}
    records = list(records or ())
    whole = assemble(records)  # long posts compared and kept in full (the duplicate rule needs the whole text)

    def parse(r):
        b = _body(r) if isinstance(r, dict) and r.get('id') in whole else None
        if b is None:
            return _parse(r, models)
        field, text = whole[r['id']]
        p = _parse(dict(r, body=dict(b, **{field: text, 'parts': 1})), models)
        if p:p['assembled'] = True
        return p
    items = [p for p in (parse(r) for r in (records or ())) if p]
    items.sort(key=lambda p: p['id'] if isinstance(p['id'], int) else 0)
    critics = set(policy.get('critic_workers') or ())
    critics |= {p['worker'] for p in items if p['role'] in CRITIC_ROLES}
    critics |= {p['worker'] for p in items if p['model'] and str(p['model']).lower() in crit_models}
    limit = (policy.get('proposer') or {}).get('max_unreviewed_claims', DEFAULT_PROPOSER['max_unreviewed_claims'])
    anyone = not critics  # nobody identifiable as critic: any non-proposer may judge

    wmodel = {str(k): v for k, v in (models or {}).items() if v}  # registered models count even before posting (review)
    for p in items:
        if p['model']:wmodel.setdefault(p['worker'], p['model'])

    # Operator 2026-10-07 (graded live critics): per-model critic weight. 'full' (default) counts;
    # 'advisory' accepts never count toward k_accept and its rejects never refute alone;
    # 'none' verdicts are ignored (the model is not a critic).
    # Operator 2026-10-09: 'block' accepts never count and its reject or objection never refutes: either
    # sends the claim to needs-repair (an extra vote that can only stop a claim, e.g. GLM: 16/30 graded right).
    weights = {str(k).lower(): str(v).lower() for k, v in (policy.get('critic_weights') or {}).items()}

    def vmodel(w, c=None):
        """The model that gave worker w's verdict on claim c (imported verdicts keep their own)."""
        return ((c or {}).get('verdict_models') or {}).get(w) or wmodel.get(w)

    def weight(w, c=None):
        return weights.get(str(vmodel(w, c) or '').lower(), 'full')

    pair = acc.get('mode') == 'pair'
    pair_models = list(acc.get('pair_models') or ('dsf', 'k3'))

    def fam(w, c):
        from .certifier_repair import reviewer_family
        return reviewer_family(w, vmodel(w, c))

    def pair_state(c):
        """Operator 2026-10-09: one critic of each pair model reviews in parallel. Both accept -> accepted;
        both reject -> refuted; both object, or a reject and an objection -> needs-repair; one accepts and
        the other rejects or objects -> 'tiebreak' (a fresh blinded tie-break session decides; its
        verdict settles the claim). The latest verdict of each side counts."""
        vr = c.get('verdict_records') or {}
        settled_at = max((vr.get(w, 0) for w in c['verdicts'] if is_tiebreak(w)), default=0)
        if settled_at and any(v in ('reject', 'object') and vr.get(w, 0) > settled_at
                              for w, v in c['verdicts'].items() if not is_tiebreak(w) and not is_cert(w)):
            return 'needs-repair'
        return pair_outcome(c['verdicts'], {w: vmodel(w, c) for w in c['verdicts']}, policy)

    cert = (policy.get('certification') or {}) if isinstance(policy.get('certification'), dict) else {}

    def cert_state(c, base):
        """Certification (operator 2026-10-09): the latest verdict of each certifier family on the current
        revision. Any reject or objection sends an accepted claim back to needs-repair (never refuted);
        certified = accepted and every eligible family accepts; the current author family is excluded."""
        latest = {}
        for w, v in c['verdicts'].items():
            if is_cert(w):
                from .certifier_repair import required_families
                if fam(w, c) in required_families(c):
                    latest[fam(w, c)] = v
        c['cert_verdicts'] = dict(latest)
        from .certifier_repair import MAX_REPAIR_DEPTH
        if c.get('repair_depth', 0) >= MAX_REPAIR_DEPTH and any(v == 'reject' for v in latest.values()):
            c['certified'] = False
            return 'refuted'
        if base != 'accepted':
            c['certified'] = False
            return base
        if c.get('repair_complete') is False:
            c['certified'] = False
            return 'needs-repair'
        if any(v in ('reject', 'object') for v in latest.values()):
            c['certified'] = False
            return 'needs-repair'
        from .certifier_repair import required_families
        required = required_families(c)
        c['cert_required'] = required
        c['certified'] = bool(required) and all(latest.get(f) == 'accept' for f in required)
        return base

    def state_of(c):
        if c.get('post_type') == 'literature':
            from .post_types import literature_state
            c['certified'] = False  # evidence is never a certified mathematical result
            state=literature_state(c)
            if state=='accepted' and c.get('literature_evidence_required') and not c.get('literature_evidence_verified'):
                return 'needs-repair'
            return state
        if pair:
            return cert_state(c, pair_state(c))
        vs = {w: v for w, v in c['verdicts'].items() if weight(w, c) != 'none'}
        full = {w: v for w, v in vs.items() if weight(w, c) == 'full'}
        a = sum(v == 'accept' for v in full.values())
        r_full = sum(v == 'reject' for v in full.values())
        r = sum(v == 'reject' for w, v in vs.items() if weight(w, c) != 'block')
        # Operator 2026-10-06: refutation wins. k_refute current rejects refute regardless of
        # accepts; acceptance needs k_accept accepts with no standing reject, and the accepts
        # may not all come from the proposer's own model when another critic model exists.
        # An advisory critic's reject refutes only together with another critic's reject.
        if r_full >= acc['k_refute'] or (r > r_full and r >= max(2, acc['k_refute'])):
            return 'refuted'
        if any(v == 'object' or (v == 'reject' and weight(w, c) == 'block') for w, v in vs.items()):
            return 'needs-repair'  # a standing objection: back to the proposer, neither refuted nor accepted
        accepters = [w for w, v in full.items() if v == 'accept']
        if len(accepters) >= acc['k_accept']:
            pm = str(c.get('proposer_model') or '').lower()
            other_models = ({str(wmodel.get(w) or '').lower() for w in (critics if not anyone else wmodel) if weight(w) == 'full'}
                            | {m for m in crit_models if weights.get(m, 'full') == 'full'}) - {pm, ''}
            if pm and other_models and acc.get('distinct_from_proposer_model', True):
                other = [w for w in accepters if str(vmodel(w, c) or '').lower() != pm]
                if not other:
                    return 'disputed' if r_full else 'open'
            return 'accepted'
        return 'disputed' if a and r_full else 'open'  # a lone advisory reject keeps the claim open for review

    claims, by_id, held, alias = {}, {}, {}, {}

    def add_claim(p):
        claims[p['key']] = dict(key=p['key'], statement=p['first'], proposer=p['worker'],
                                revision_author=p['worker'], revision_author_model=p['model'],
                                repair_depth=p.get('repair_depth') or 0, repair_author_families=p.get('repair_author_families') or [], repair_of=p.get('repair_of'), repair_verdict=p.get('repair_verdict'), repair_complete=p.get('repair_complete'),
                                proposer_model=p['model'], records=dict(proposal=p['id'], verdicts=[]),
                                verdicts={}, transitions=[], self_checked=bool(SELF_CHECK.search(p['text'])),
                                authors=[p['worker']], text=p['text'], title=_title(p), revision=0, revisions=[],
                                restatements=[], superseded_verdicts=[], route_key=p['key'],
                                post_type=p.get('post_type') or 'reasoning', literature=p.get('literature'), premise_refs=p.get('premise_refs',[]), literature_evidence_required=p.get('literature_evidence_required',False), literature_evidence_verified=p.get('literature_evidence_verified',False), source_bindings=[], literature_refs=p.get('literature_refs', []), kind=p['kind'], has_finding=p['kind'] == 'finding', at=p.get('at'), verdict_models={})
        waiting = [k for k, c in claims.items() if k != p['key'] and c['proposer'] == p['worker']
                   and state_of(c) == 'open']
        queued = bool(limit) and len(waiting) >= limit
        claims[p['key']].update(queued=queued, route_after=waiting if queued else [])
        if p['id'] is not None:
            by_id[p['id']] = p['key']

    def import_claim(p):
        """A claim left unsettled by an earlier session, re-posted by the controller at the start of
        a continuation: it keeps its proposer and its standing verdicts (each earlier critic slot
        stays one voter, so only a distinct critic's verdict adds to them) and is reviewed and
        settled here like any claim; later posts restating it attach to it (duplicate rule)."""
        m = p['imported']
        key = str(m.get('key') or p['key'] or 'imported:%s' % p['id'])
        if key in claims:
            key = '%s [%s]' % (key, m.get('origin') or p['id'])
        proposer = str(m.get('proposer') or 'unknown')
        vm = m.get('voter_models') if isinstance(m.get('voter_models'), dict) else {}
        verdicts = m.get('verdicts') if isinstance(m.get('verdicts'), dict) else {}
        verdicts = {str(w): v for w, v in verdicts.items() if v in ('accept', 'reject', 'object')}
        given = {w: str(vm.get(w) or wmodel.get(w) or '') for w in verdicts}  # each earlier verdict keeps the model that gave it
        verdicts = {w: v for w, v in verdicts.items() if weight(w, dict(verdict_models=given)) != 'none'}
        authors = [str(a) for a in (m.get('authors') if isinstance(m.get('authors'), list) else None) or [proposer]]
        if m.get('proposer_model'):wmodel.setdefault(proposer, str(m['proposer_model']))
        title = [str(x) for x in m['title']] if isinstance(m.get('title'), list) else _title(p)
        bindings=m.get('source_bindings') or [dict(origin='legacy-unbound-source:%s'%i,text_hash='missing') for i in (m.get('literature_refs') or [])]
        if not isinstance(bindings,list) or any(not isinstance(b,dict) or set(b)!={'origin','text_hash'} or not all(isinstance(v,str) for v in b.values()) for b in bindings):
            bindings=[dict(origin='malformed-source-binding',text_hash='missing')]
        claims[key] = dict(key=key, statement=str(m.get('statement') or p['first'])[:200], proposer=proposer,
                           revision_author=m.get('revision_author'), revision_author_model=m.get('revision_author_model'),
                           repair_depth=m.get('repair_depth') or 0, repair_author_families=m.get('repair_author_families') or [], repair_of=m.get('repair_of'), repair_verdict=m.get('repair_verdict'), repair_complete=m.get('repair_complete'),
                           proposer_model=m.get('proposer_model'), records=dict(proposal=p['id'], verdicts=[]),
                           verdicts=verdicts, transitions=[], self_checked=bool(m.get('self_checked')),
                           authors=authors, text=p['text'], title=title, revision=0, revisions=[],
                           restatements=[], superseded_verdicts=[], route_key=key,
                           post_type=m.get('post_type') or 'reasoning', literature=m.get('literature'),premise_refs=[], premise_bindings=m.get('premise_bindings') or ([dict(origin='legacy-unbound-premise',text_hash='missing')] if m.get('premise_refs') else []), literature_evidence_required=m.get('literature_evidence_required',False), literature_evidence_verified=m.get('literature_evidence_verified',False), literature_refs=[], source_bindings=bindings, kind=m.get('kind') or 'finding', has_finding=bool(m.get('has_finding', True)), at=p.get('at'),
                           imported=dict(m), queued=False, route_after=[], reviewed_by=[], assembled=bool(p.get('assembled')),
                           verdict_models={w: given[w] for w in verdicts if given.get(w)})
        if p['id'] is not None:
            by_id[p['id']] = key

    def absorb(p, forced=None):
        """Attach a post that restates an existing claim; True when attached (see POINTER).
        forced: the claim a same-task post already belongs to; it is a restatement when it adds
        nothing, a revision only when it declares itself one, else (False) a follow-up as before."""
        target = None
        if p['worker'] in critics and not anyone and p.get('post_type') not in ('reasoning', 'literature'):
            return False  # a critic's post never revises someone's claim
        n, rest = _pointer(p['text'] or p['first'])
        n = p.get('revision_of') if p.get('revision_of') is not None else n
        if forced is not None:
            target = forced
            if n is not None and not same_content(rest, claims[forced]['text']) and by_id.get(n) not in (None, forced):
                return False  # declares a repair of some other claim: leave as before
        elif n is not None and by_id.get(n) in claims:
            target = by_id[n]
        else:
            t = _title(p)
            if len(t) >= TITLE_MIN_WORDS:
                hits = [k for k, c in claims.items() if same_title(t, c['title'])]
                if len(hits) == 1:
                    target = hits[0]  # ambiguous (several matches): a new claim
        if target is None:
            return False
        c = claims[target]
        st = state_of(c)
        changed = p.get('parts', 1) > 1 or not same_content(rest, c['text'])  # a chunked post is never called unchanged
        if st == 'refuted':
            # Only a post that explicitly answers the refutation repairs it: it names a standing
            # rejecting verdict's record, or says what it does about the refutation.
            standing = {str(i) for w, i in c.get('verdict_records', {}).items() if c['verdicts'].get(w) == 'reject'}
            neg = lambda i: re.search(r"\b(?:not|never|no|n't|without|fails?\s+to|cannot|doesn't|does\s+not)\b[^.\n]{0,40}$",
                                    rest[max(0, i - 50):i], re.I)
            cites = any(not neg(m.start()) for x in standing
                        for m in re.finditer(r"(?:#|board\s*:\s*|record\s+#?|verdict\s+#?)\s*%s\b" % x, rest, re.I))
            affirmed = any(not neg(m.start()) for m in ADDRESSES.finditer(rest))
            changed = changed and (cites or affirmed)
        if p['id'] is not None:
            by_id[p['id']] = target
        if p['key'] and p['key'] not in claims:
            alias[p['key']] = target  # a task-only verdict on the revised title finds the claim
        if not changed:
            c['restatements'].append(dict(record=p['id'], author=p['worker'], at=p.get('at'), state=st))
            return True
        if p.get('post_type') in ('reasoning', 'literature') and p.get('revision_of') is None:
            return False  # typed result text changes require an admitted exact revision link
        c['revision'] += 1
        c['revisions'].append(dict(record=p['id'], author=p['worker'], at=p.get('at'), state_before=st,
                                   verdicts_before=dict(c['verdicts'])))
        if c['verdicts']:
            c['superseded_verdicts'].append(dict(revision=c['revision'] - 1, verdicts=dict(c['verdicts'])))
        c['verdicts'] = {}
        c['verdict_models'] = {}
        c['verdict_records'] = {}  # a revision is reviewed afresh (its tie-break never sees the old critiques)
        c['reviewed_by'] = []
        from .certifier_repair import author_family, reviewer_family
        excluded = set(c.get('repair_author_families') or []) | set(p.get('repair_author_families') or [])
        if c.get('repair_of') is not None: excluded.add(author_family(c))
        if is_cert(p['worker']) or is_tiebreak(p['worker']): excluded.add(reviewer_family(p['worker'], p['model']))
        c.update(repair_depth=max(c.get('repair_depth') or 0, p.get('repair_depth') or 0),
                 repair_author_families=sorted(excluded), revision_author=p['worker'], revision_author_model=p['model'],
                 repair_of=(p.get('repair_of') if p.get('repair_of') is not None else
                            p.get('revision_of') if is_cert(p['worker']) or is_tiebreak(p['worker']) else None),
                 repair_verdict=p.get('repair_verdict'), repair_complete=p.get('repair_complete'),
                 statement=p['first'] or c['statement'], text=p['text'], title=_title(p) or c['title'], at=p.get('at', c.get('at')),
                 post_type=p.get('post_type') or 'reasoning', literature=p.get('literature'), premise_refs=p.get('premise_refs',[]), literature_evidence_required=p.get('literature_evidence_required',False), literature_evidence_verified=p.get('literature_evidence_verified',False), source_bindings=[], literature_refs=p.get('literature_refs', []),
                 route_key='%s @r%d' % (c['key'], c['revision']))
        if p['worker'] not in c['authors']:
            c['authors'].append(p['worker'])
        if p['kind'] == 'finding':
            c['has_finding'] = True
        if SELF_CHECK.search(p['text']):
            c['self_checked'] = True
        if st != 'open':
            c['transitions'].append(dict(frm=st, to='open', record=p['id'], at=p.get('at'), critic=None, verdict='revision',
                                         accepts=[], rejects=[]))
        return True

    for p in items:
        if p.get('post_type') in ('note', 'verdict') and not p['verdict']:
            continue
        if p.get('post_type') == 'note':
            continue  # a note is never a verdict or a revision
        if p.get('repair_alternative') and p.get('post_type') == 'reasoning':
            add_claim(p)
            continue  # concurrent repair of an older revision: an independently reviewed alternative
        if p.get('revision_of') is not None:
            p = dict(p, re=p['revision_of'])
        if p.get('imported'):
            try:
                import_claim(p)
            except Exception:
                pass  # malformed import metadata: the record stays a plain controller note
            continue
        if p['worker'] == 'controller' and p['kind'] == 'comment' and p['re'] in by_id:
            c = claims.get(by_id[p['re']])
            if c is not None and c.get('imported') and c['records']['proposal'] == p['re']:
                if not c['revision'] and not c.get('assembled'):
                    c['text'] = c['text'] + p['text']  # an incomplete set of parts: what is there, in order
                if p['id'] is not None:
                    by_id[p['id']] = c['key']
                continue
        bound = (policy.get('bound_reviews') or {}).get(p['worker'])
        if bound is not None and p['verdict'] and bound in by_id:
            if p['re'] is not None and p['re'] != bound:
                claims[by_id[bound]].setdefault('stale_verdicts', []).append(p['id'])
                continue  # an explicit wrong target is never silently redirected
            p = dict(p, re=bound)
        target = None
        if p['re'] is not None and p['re'] in by_id:
            target = by_id[p['re']]
        elif p['key'] and p['key'] in claims:
            target = p['key']
        elif p['key'] and alias.get(p['key']) in claims and p['verdict']:
            target = alias[p['key']]
        if target is not None and p.get('revision_of') is not None and p.get('post_type') in ('reasoning', 'literature'):
            if absorb(p, forced=target): continue
        if target is not None and p['kind'] == 'finding' and _voter(p['worker']) in {_voter(a) for a in claims[target].get('authors', ())}:
            claims[target]['has_finding'] = True  # an author's result on its plan (also in the no-critic fallback)
        if target is not None and p['verdict'] and weight(p['worker']) == 'none' and (anyone or p['worker'] in critics):
            claims[target].setdefault('ignored_verdicts', []).append(p['id'])
            if p['id'] is not None:
                by_id[p['id']] = target
            continue  # a model that is not a critic: its verdict post is kept on the board, not counted
        if target is not None and p['verdict'] and p['re'] is not None and (bound is not None or p.get('post_type') == 'verdict' or is_cert(p['worker']) or is_tiebreak(p['worker'])):
            _c = claims[target]
            _cur = (_c['revisions'][-1]['record'] if _c.get('revisions') else None) or _c['records']['proposal']
            if p['re'] != _cur:
                _c.setdefault('stale_verdicts', []).append(p['id'])  # reviewed an older revision (review ev4)
                if p['id'] is not None:
                    by_id[p['id']] = target
                continue
        if target is not None and p['verdict'] and (anyone or p['worker'] in critics):
            c = claims[target]
            from .certifier_repair import required_families
            self_review = ((fam(p['worker'], c) not in required_families(c) or
                            _voter(p['worker']) == _voter(c.get('revision_author') or c['proposer'])) if is_cert(p['worker']) else
                           _voter(p['worker']) in {_voter(a) for a in c.get('authors') or [c['proposer']]})
            if self_review:
                if p['kind'] != 'comment' and p['key'] == target and p['role'] not in CRITIC_ROLES and absorb(p, forced=target):
                    continue  # an author's same-task repost: restatement or declared revision (no-critic fallback too)
                if SELF_CHECK.search(p['text']):
                    c['self_checked'] = True
                if p['id'] is not None:
                    by_id[p['id']] = target  # its continuation parts resolve to the claim
                continue  # critics never judge their own claims
            previous = [(w, v) for w, v in c['verdicts'].items() if _voter(w) == _voter(p['worker'])]
            if p['verdict'] == 'accept' and any(v in ('object', 'reject') for _, v in previous):
                objection_ids = [(c.get('verdict_records') or {}).get(w) for w, v in previous if v in ('object', 'reject')]
                resolves = re.search(r'^\s*Resolves-objection:\s*#?(\d+)\s*$', p['text'], re.I | re.M)
                explanation = re.search(r'^\s*Explanation:\s*(\S.+)$', p['text'], re.I | re.M)
                if not (p.get('explicit_verdict') and resolves and int(resolves.group(1)) in objection_ids and explanation):
                    c.setdefault('ignored_verdicts', []).append(p['id'])
                    continue
            c['records']['verdicts'].append(p['id'])
            before = state_of(c)
            # Successive sessions of one standing critic slot are one voter: the latest verdict
            # replaces (or retracts) the slot's earlier one.
            for w in [w for w in c['verdicts'] if w != p['worker'] and _voter(w) == _voter(p['worker'])]:
                c['verdicts'].pop(w)
                (c.get('verdict_models') or {}).pop(w, None)
            c.setdefault('reviewed_by', [])
            if p['worker'] not in c['reviewed_by']:
                c['reviewed_by'].append(p['worker'])  # this revision; a retraction still counts as reviewed
            if p['verdict'] == 'retract':
                c['verdicts'].pop(p['worker'], None)
                (c.get('verdict_models') or {}).pop(p['worker'], None)
            else:
                c['verdicts'][p['worker']] = p['verdict']
                if wmodel.get(p['worker']):
                    c.setdefault('verdict_models', {})[p['worker']] = wmodel.get(p['worker'])  # this verdict's own model (imports keep it)
                else:
                    (c.get('verdict_models') or {}).pop(p['worker'], None)
                c.setdefault('verdict_records', {})[p['worker']] = p['id']
            after = state_of(c)
            if before != after:
                # Coordinator 2026-10-06: overturned refutations and accepted->refuted flips are
                # logged so the refutation threshold can be judged from data later.
                c['transitions'].append(dict(frm=before, to=after, record=p['id'], at=p.get('at'), critic=p['worker'],
                                             verdict=p['verdict'], accepts=sorted(w for w, v in c['verdicts'].items() if v == 'accept'),
                                             rejects=sorted(w for w, v in c['verdicts'].items() if v == 'reject')))
            continue
        head = held.get(p['re']) if p['kind'] == 'comment' and target is None and p['re'] is not None else None
        if head is not None and head['worker'] == p['worker'] and SELF_CHECK.search(p['text']):
            head['held_self_check'] = True  # a continuation part's Self-check counts once promoted
        if head is not None and head['worker'] == p['worker'] and p['explicit_claim'] and head['key'] not in claims:
            # A long note stored in parts whose "Claim:" line is in a continuation part.
            del held[p['re']]
            if absorb(head):
                if p['id'] is not None:
                    by_id[p['id']] = by_id.get(head['id'])
                continue
            add_claim(head)
            if head.get('held_self_check'):
                claims[head['key']]['self_checked'] = True
            if p['id'] is not None:
                by_id[p['id']] = head['key']
            continue
        if p['kind'] != 'comment' and p['key'] and p['key'] in claims and target == p['key'] \
                and not (p['worker'] in critics and not anyone) and p['role'] not in CRITIC_ROLES and absorb(p, forced=target):
            continue  # same task: a pure restatement, or a declared revision
        if p['kind'] == 'comment' or not p['key'] or p['key'] in claims:
            if target is not None and p['kind'] == 'finding' and p['worker'] in claims[target]['authors']:
                claims[target]['has_finding'] = True  # a plan ("Claim: <task>") followed by its result
            if target is not None and p['id'] is not None:
                by_id[p['id']] = target
                c = claims[target]
                if p['worker'] == c['proposer'] and SELF_CHECK.search(p['text']):
                    c['self_checked'] = True  # continuation part or follow-up by the proposer
            continue
        if p['worker'] in critics and p['verdict'] in ('reject', 'retract') and not anyone:
            continue  # a critic's verdict on nothing known is not a claim
        if p['role'] in CRITIC_ROLES and p['verdict']:
            continue  # operator 2026-10-07: a registered critic's verdict post is never a claim to review
        # Literature findings (Status lines) are claims like any other and are reviewed (operator
        # 2026-10-07: 15 of 17 went unreviewed); a literature member's plain notes stay notes.
        if p['role'] in NON_PROPOSER_ROLES and not p['explicit_claim'] and not (p['role'] == 'literature' and p['kind'] == 'finding'):
            if p['id'] is not None:
                held[p['id']] = p
            continue  # a critic's or literature member's note (orientation, status) is kept on the board, not a claim
        if p.get('post_type') == 'verdict' or (p['verdict'] and p['re'] is not None):
            continue
        if p.get('revision_of') is not None and p['revision_of'] in by_id:
            if absorb(p, forced=by_id[p['revision_of']]):
                continue
        if absorb(p):
            continue  # restates an existing claim: a revision of it, or a restatement (no new claim)
        add_claim(p)
    for c in claims.values():
        c['accepts'] = sum(v == 'accept' and weight(w, c) == 'full' for w, v in c['verdicts'].items())  # counting accepts only
        c['accept_models'] = sorted({str(MODEL_FAMILY.get(str(vmodel(w, c)).lower(), vmodel(w, c))).lower() for w, v in c['verdicts'].items()
                                     if v == 'accept' and weight(w, c) == 'full' and vmodel(w, c)})  # an unknown model never adds a model
        c['advisory'] = sorted(w for w in c['verdicts'] if weight(w, c) != 'full')
        c['rejects'] = sum(v == 'reject' for v in c['verdicts'].values())
        c['objections'] = sum(v == 'object' for v in c['verdicts'].values())
        c['state'] = state_of(c)
        if pair:
            c['critiques'] = {w: v for w, v in c['verdicts'].items() if not is_tiebreak(w)}
    # A corrected/withdrawn source revision invalidates its applicability claims, even
    # when their own prose and old reviewer verdicts did not change.
    from .global_planner import current
    active = {}
    for r in records:
        if not isinstance(r,dict):continue
        b = _body(r) or {}
        if r.get('kind') == 'invalidation': active[b.get('cause')] = b.get('affected_records') or [b.get('root')]
        elif r.get('kind') == 'challenge-resolution': active.pop(b.get('cause'),None)
    invalid_records={i for ids in active.values() for i in ids}
    invalid_records.update(r['id'] for r in records if isinstance(r,dict) and r.get('valid') is False)
    for c in claims.values():
        if current(c) in invalid_records:
            c['dependency_invalid'] = [current(c)]
            c['state'] = 'needs-repair'; c['certified'] = False
    by_current = {current(c):c for c in claims.values()}
    for c in claims.values():
        refs = c.get('literature_refs') or []
        from .global_planner import hashed
        origins={}
        for x in claims.values():
            if x.get('post_type')=='literature':
                meta=x.get('imported') or {}
                for origin in [meta.get('origin')]+(meta.get('aliases') or []):
                    if origin:origins[origin]=x
        invalid_bindings=[b['origin'] for b in c.get('source_bindings', []) if b.get('origin') not in origins or origins[b['origin']]['state']!='accepted' or hashed(origins[b['origin']]['text'])!=b['text_hash']]
        invalid = [i for i in refs if i not in by_current or by_current[i].get('post_type') != 'literature' or by_current[i]['state'] != 'accepted']
        invalid += invalid_bindings
        if invalid:
            c['dependency_invalid'] = invalid
            c['state'] = 'needs-repair'; c['certified'] = False
    # Fixed point: reopening any source/reasoning premise reopens every dependent
    # conclusion, regardless of ledger insertion order or proof depth.
    premise_origins={}
    for c in claims.values():
        meta=c.get('imported') or {}
        for origin in [meta.get('origin')]+(meta.get('aliases') or []):
            if origin:premise_origins[origin]=c
    for _ in range(len(claims)):
        changed=False
        for c in claims.values():
            invalid=[i for i in c.get('premise_refs',[]) if i not in by_current or by_current[i]['state']!='accepted']
            invalid += [b['origin'] for b in c.get('premise_bindings',[]) if b.get('origin') not in premise_origins or premise_origins[b['origin']]['state']!='accepted' or hashed(premise_origins[b['origin']]['text'])!=b['text_hash']]
            if invalid and (c['state']!='needs-repair' or c.get('certified')):
                c['dependency_invalid']=sorted(set(c.get('dependency_invalid',[])+invalid),key=str)
                c['state']='needs-repair';c['certified']=False;changed=True
        if not changed:break
    return claims


def _slot_family(worker, pair_models):
    m = STANDING_SESSION.fullmatch(str(worker))
    slot = (m.group(1) if m else str(worker)).split(':', 1)[-1]
    return next((str(MODEL_FAMILY.get(p, p)).lower() for p in pair_models if slot == p or slot.startswith(p + '-')), None)


def pair_outcome(verdicts, models, policy):
    """The pair rule on a verdict set ({worker: verdict}, {worker: model}); shared by the ledger and the
    continuation import. Returns accepted / refuted / needs-repair / tiebreak / open."""
    acc = (policy or {}).get('acceptance') or {}
    pms = list(acc.get('pair_models') or ('dsf', 'k3'))
    tb = [v for w, v in verdicts.items() if is_tiebreak(w)]
    if tb:
        return {'accept': 'accepted', 'reject': 'refuted'}.get(tb[-1], 'needs-repair')
    side = {}
    fams = {str(MODEL_FAMILY.get(p, p)).lower(): p for p in pms}
    for w, v in verdicts.items():
        if is_cert(w) or str(w).startswith('critic:select-'):
            continue
        m = str((models or {}).get(w) or '').lower()
        f = str(MODEL_FAMILY.get(m, m)).lower() if m else _slot_family(w, pms)
        if f in fams:
            side[f] = v
    if len(side) < 2:
        return 'open'
    a, b = side.values()
    if a == b:
        return {'accept': 'accepted', 'reject': 'refuted'}.get(a, 'needs-repair')
    return 'tiebreak' if 'accept' in (a, b) else 'needs-repair'


def is_cert(worker):
    """A certification session (roster certifiers: slot 'critic:cert-<model>...')."""
    return str(worker).startswith('critic:cert-')


def is_tiebreak(worker):
    """A tie-break session (roster tiebreakers: slot 'critic:tiebreak-<model>...')."""
    return str(worker).startswith('critic:tiebreak-')


WATCHED_FLIPS = (('refuted', None), ('accepted', 'refuted'), (None, 'needs-repair'))


def watched_transitions(claims):
    """Overturned refutations (refuted -> anything) and accepted -> refuted flips."""
    out = []
    for c in claims.values():
        was_accepted = False
        for t in c.get('transitions', ()):
            if t['frm'] == 'refuted':
                out.append(dict(t, claim=c['key'], proposer=c['proposer'], flip='refutation-overturned'))
            elif t['to'] == 'refuted' and (was_accepted or t['frm'] == 'accepted'):
                out.append(dict(t, claim=c['key'], proposer=c['proposer'], flip='accepted-then-refuted'))
            if t['to'] == 'needs-repair' and t['frm'] != 'needs-repair':
                out.append(dict(t, claim=c['key'], proposer=c['proposer'], flip='needs-repair'))
            if t['to'] == 'accepted':was_accepted = True
    return out


def min_accepts(policy=None):
    policy = policy or default_policy()
    n = (policy.get('writer') or {}).get('min_critic_accepts')
    return n if isinstance(n, int) and n > 0 else dict(DEFAULT_ACCEPTANCE, **(policy.get('acceptance') or {}))['k_accept']


def min_accept_models(policy=None):
    n = ((policy or {}).get('writer') or {}).get('min_accept_models')
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1


def _model_spread(claim):
    m = claim.get('accept_models')
    return len(m) if isinstance(m, (list, tuple, set)) else None


def writer_verified(claim, policy=None):
    """Writer rule (items 4/6): accepted, with at least writer.min_critic_accepts accepts, from
    critics of at least writer.min_accept_models distinct models (a claim from an older ledger
    without accept_models counts as one model when the policy asks for more)."""
    if claim.get('repair_complete') is False:
        return False  # an explicit unresolved gap is not a complete repaired proof
    if claim.get('post_type') == 'literature':
        return False  # source evidence cannot be published as a proved reasoning result
    if ((policy or {}).get('acceptance') or {}).get('mode') == 'pair':
        if ((policy or {}).get('certification') or {}).get('enabled'):
            return claim.get('state') == 'accepted' and bool(claim.get('certified'))  # proved only once certified
        return claim.get('state') == 'accepted'  # both pair critics, or the tie-break, accepted
    if not (claim.get('state') == 'accepted' and claim.get('accepts', 0) >= min_accepts(policy)):
        return False
    need = min_accept_models(policy)
    return need <= 1 or (_model_spread(claim) or 1) >= need


def needs_audit(claim, policy=None):
    """Accepted by enough critics, but from fewer models than the writer rule needs: routed to an
    auditor of another model (policy audit.models) before the writer may use it."""
    return (claim.get('post_type') != 'literature' and claim.get('state') == 'accepted' and claim.get('accepts', 0) >= min_accepts(policy)
            and not writer_verified(claim, policy))


def routable(claim, policy=None):
    """A claim some critic should still look at: open for review, or accepted awaiting audit."""
    from .state_page import reviewable
    return reviewable(claim) or needs_audit(claim, policy)

LEDGER_RULE_MARK = '<!-- ledger-rule: min_accept_models=%d -->'  # machine-readable: the rule this ledger was written under
LEDGER_RULE_RE = re.compile(r'^<!-- ledger-rule: min_accept_models=(\d+) -->$', re.M)


def ledger_markdown(claims, policy=None):
    """CLAIMS-LEDGER.md for writers: one row per claim."""
    need = min_accepts(policy)
    def cell(x):
        return ' '.join(str(x).split()).replace('|', '\\|')
    models = min_accept_models(policy)
    out = ['# Claims ledger', '',
           'Verified = listed here as accepted (at least %d critic accepts%s). Every other claim (open, disputed, '
           'refuted, needs-repair, insufficient-accepts, awaiting-audit) is unverified; needs-repair means a critic objected '
           '(e.g. unsourced or incomplete) and the proposer has not yet repaired it; awaiting-audit means its accepts all '
           'came from critics of one model and no critic of another model has confirmed it yet.'
           % (need, ', from critics of at least %d different models' % models if models > 1 else '')
           + (' With certification on, awaiting-audit also means accepted by both review critics but not yet certified by the '
              'certification check: state it only conditional on the unverified results.'
              if ((policy or {}).get('certification') or {}).get('enabled') else ''), '',
           'Literature rows are source evidence only: source-checked never means a proved reasoning result. Notes and verdicts are not result claims.', '',
           LEDGER_RULE_MARK % models, '',
           '| Claim | Type | State | Accepts | Rejects | Proposal record | Verdict records |', '|---|---|---|---|---|---|---|']
    for c in sorted(claims.values(), key=lambda c: (c['records']['proposal'] if isinstance(c['records']['proposal'], int) else 0)):
        state = ('source-checked' if c['state'] == 'accepted' else c['state']) if c.get('post_type') == 'literature' else 'accepted' if writer_verified(c, policy) else ('awaiting-audit' if needs_audit(c, policy) else
                                                               'insufficient-accepts' if c['state'] == 'accepted' else c['state'])
        out.append('| %s | %s | %s | %d | %d | %s | %s |' % (cell(c['statement'] or c['key']), c.get('post_type', 'reasoning'), state, c.get('accepts', 0), c.get('rejects', 0),
                   cell((c['revisions'][-1]['record'] if c.get('revisions') else c['records']['proposal'])), cell(', '.join(str(i) for i in c['records']['verdicts']) or '-')))
    if not claims:
        out.append('| (no claims) | - | - | 0 | 0 | - | - |')
    return '\n'.join(out) + '\n'
