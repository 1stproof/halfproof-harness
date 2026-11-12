"""Per-model role prompts and per-swarm model lists.

roles/<swarm>/<model>.md : text appended to that model's research-worker / open-member
brief under "Your role:" (empty or missing file = no change).
roles/<swarm>/models.json : {"workers": [...], "members": [...]}; an absent or empty list
means no restriction. Model names match the harness keys (dsf, dsp, k3, qwen24t, glm),
compared case-insensitively. Files are read once per process.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / 'roles'
FOLLOW_UP = ("\nEnd every turn with 'Suggested follow-ups:', listing 1 to 5 one-line tasks you would hand to someone next.")


@lru_cache(maxsize=None)
def role_text(swarm, model, root=None):
    d = Path(root) if root else ROOT
    d = d / str(swarm)
    if not d.is_dir():
        return ''
    want = str(model).lower() + '.md'
    for p in d.iterdir():
        if p.name.lower() == want and p.is_file():
            return p.read_text().strip()
    return ''


@lru_cache(maxsize=None)
def model_list(swarm, key, root=None):
    """Lower-cased allowed models for key ('workers'/'members'), or None = unrestricted.
    For 'workers', a roster in policy.json restricts to its ideas and critic models
    (intersected with models.json 'workers' when both are set)."""
    p = (Path(root) if root else ROOT) / str(swarm) / 'models.json'
    try:
        data = json.loads(p.read_text() or '{}')
    except (OSError, ValueError):
        data = {}
    x = data.get(key) if isinstance(data, dict) else None
    out = frozenset(str(m).lower() for m in x) if x else None
    if key == 'workers':
        r = roster(swarm, root)
        if r is not None and (r['ideas'] or r['critics'] or r['literature']):
            rs = frozenset(r['ideas'] + r['critics'] + r['literature'] + r.get('auditors', []) + r.get('explorers', []) + r.get('tiebreakers', [])
                            + r.get('selectors', []) + r.get('certifiers', []))
            out = rs if out is None else out & rs
    return out


# ------------------------------------------------------------------ roster
MODEL_KEYS = ('dsf', 'dsp', 'k3', 'qwen24t', 'glm')
ROSTER_LISTS = (('ideas', 'ideas'), ('critics', 'critic'), ('literature', 'literature'),
                # Operator 2026-10-08: off the critical path. auditors: standing slots that only review claims
                # already accepted by critics of one model (accuracy matters, latency does not); explorers:
                # standing slots that post extra exploratory ideas nobody waits for (never critics).
                ('auditors', 'auditor'), ('explorers', 'explorer'),
                # Operator 2026-10-09: tie-break slots decide a claim its two pair critics disagree on.
                ('tiebreakers', 'tiebreak'),
                # Operator 2026-10-09: certification layer (non-blocking): a global proof planner selects load-bearing
                # verified claims; certifier pools check them one claim per session.
                ('selectors', 'select'), ('certifiers', 'certify'))
STANDING_ROLES = ('critic', 'auditor', 'explorer', 'tiebreak', 'select', 'certify')  # staffed by the harness (v2/standing_critics.py), never by the coordinator
CRITIC_BRIEF = (
    'You are a CRITIC on an open research team working on PROBLEM.md. There is no coordinator; the team '
    'coordinates only through the shared notes (the board, via board_call).\n'
    'Your job is to review claims and findings posted by other members. Read the notes (board_call changes, '
    'read_record, open_claims), pick a claim or finding that has fewer than two verdicts, and check every step, '
    'special cases and any cited source. Post each verdict with board_post {text}; the first lines must include "Post-type: verdict" and '
    '"Status: ACCEPT" or "Status: REJECT" and "Re: <record id>" (the id of the claim or finding you reviewed). '
    'For REJECT name the exact failing step. Do not attach replacement calculations or your own results to a verdict. '
    'Never review your own claims. Claims the harness routes to you (a message headed "Claims routed to you for review") '
    'come first; each routed claim\'s full record is in /work/routed/<record id>.md, so read that file rather than paging the board. Re-read the notes before each step and keep reviewing until your session ends.')
POST_LINE = ('Use board_post only for findings, claims, verdicts and requests; never post tool names, tool calls or '
             'reading notes.')
from .post_types import POST_CONTRACT
LITERATURE_RULES = 'LITERATURE: source evidence only.\n' + POST_CONTRACT



def roster(swarm, root=None):
    """policy.json 'roster' normalized, or None when absent/unreadable:
    {ideas: [model], critics: [model], literature: [model], writer: model|None, backup_writer: model|None}.
    Unknown model keys are dropped; a model may repeat (several slots). Read once per file."""
    from .claim_ledger import policy_overrides
    ov = policy_overrides()  # a run's --policy-overrides may change the roster (e.g. the second critic model)
    over = json.dumps({k: ov[k] for k in ('roster', 'audit', 'certification') if isinstance(ov.get(k), dict)}, sort_keys=True)
    r = _roster_at(str((Path(root) if root else ROOT) / str(swarm) / 'policy.json'), over)
    return json.loads(r) if r is not None else None


@lru_cache(maxsize=None)
def _roster_at(path, over='{}'):
    p = Path(path)
    try:
        data = json.loads(p.read_text() or '{}')
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and over not in ('{}', None):
        from .claim_ledger import _deep_merge
        data = _deep_merge(data, json.loads(over))
    r = data.get('roster') if isinstance(data, dict) else None
    if not isinstance(r, dict):
        return None
    out = {}
    for key, _ in ROSTER_LISTS:
        x = r.get(key) or []
        x = x if isinstance(x, list) else [x]
        out[key] = [str(m).lower() for m in x if str(m).lower() in MODEL_KEYS]
    for key in ('writer', 'backup_writer', 'coordinator'):
        m = str(r.get(key) or '').lower()
        out[key] = m if m in MODEL_KEYS else None
    # Critic slots are standing members staffed by the harness (default when there are critics).
    sc = r.get('standing_critics')
    out['standing_critics'] = bool(out['critics']) and (sc if isinstance(sc, bool) else True)
    # Operator 2026-10-08: the auditor pool size is a policy parameter (audit.pool_size), its model the roster's.
    ps = (data.get('audit') or {}).get('pool_size') if isinstance(data.get('audit'), dict) else None
    if isinstance(ps, int) and not isinstance(ps, bool) and ps >= 0 and out['auditors']:
        out['auditors'] = [out['auditors'][0]] * ps
    # Fill eligible-family replacements and the optional third certifier from the roster.
    count = (data.get('certification') or {}).get('required_certifiers')
    if count in (2, 3) and out['certifiers']:
        for model in ('qwen24t', 'glm', 'k3', 'dsp'):
            if model not in out['certifiers']:
                out['certifiers'].append(model)
    if not out['standing_critics']:
        out['auditors'], out['explorers'], out['tiebreakers'], out['selectors'], out['certifiers'] = [], [], [], [], []  # standing slots only exist with standing critics
    return json.dumps(out)


def roster_slots(r):
    """[(slot_id, model, role)] for a normalized roster; slot ids repeat a model as 'k3', 'k3#2' within a role."""
    out = []
    for key, role in ROSTER_LISTS:
        seen = {}
        pre = {'auditor': 'audit-', 'explorer': 'explore-', 'tiebreak': 'tiebreak-', 'select': 'select-', 'certify': 'cert-'}.get(role, '')  # never the id of a critic slot of that model
        for m in (r or {}).get(key, []):
            seen[m] = seen.get(m, 0) + 1
            out.append((pre + (m if seen[m] == 1 else '%s#%d' % (m, seen[m])), m, role))
    return out


def ideas_text(swarm, model, root=None):
    """The model's role file without labelled non-ideas sections (CRITIC:, LITERATURE:, ...)
    and without lead-in sentences that only introduce such a section."""
    text = role_text(swarm, model, root)
    keep = []
    for para in re.split(r'\n\s*\n', text):
        para = para.strip()
        m = re.match(r'([A-Z][A-Z -]{2,}):', para)
        if m and m.group(1).strip() != 'IDEAS':
            continue
        sentences = re.split(r'(?<=[.:!?])\s+', para)
        para = ' '.join(x for x in sentences if not x.endswith(':') or x.startswith('IDEAS:'))
        if para:
            keep.append(para)
    return '\n\n'.join(keep)


def literature_text(swarm, model, root=None):
    return LITERATURE_RULES


CRITIC_ROLE = ('CRITIC: review claims and findings posted by other workers; check every step, special cases and any '
               'cited source. Post each verdict with board_post {text}; the first lines must include "Post-type: verdict" and "Status: ACCEPT" or '
               '"Status: REJECT" and "Re: <record id>". For REJECT name the exact failing step. Do not attach replacement '
               'calculations or your own results to a verdict. Never review your own claims. Your work is reviewing the '
               'claims the harness routes to you (messages headed "Claims routed to you for review"); each routed claim\'s full record is in /work/routed/<record id>.md, so read that file rather than paging the board; do not run a research '
               'branch of your own. When nothing is routed to you, end the session. Review only your reserved revision.')


def led_role_brief(brief, swarm, model, role, root=None):
    """Led-swarm research worker brief for a roster role (ideas / critic / literature)."""
    if role == 'critic':
        text = CRITIC_ROLE
    elif role == 'literature':
        text = literature_text(swarm, model, root)
    else:
        text = ideas_text(swarm, model, root)
    if role == 'ideas':
        from .proposer_controls import self_check_line
        check = self_check_line(swarm, model, root or ROOT, role='ideas')
        if check:brief = brief + '\n' + check
    from .post_types import POST_CONTRACT
    return brief + ('\n\nYour role:\n' + text if text else '') + ('\n' + POST_CONTRACT if POST_CONTRACT not in text else '') + FOLLOW_UP


LAUNCH_ROLES = ('ideas', 'critic', 'literature')


def launch_role(value):
    """Lenient reading of a coordinator's requested roster role: 'ideas' | 'critic' |
    'literature', or None when unreadable."""
    v = ' '.join(str(value or '').lower().replace('_', ' ').replace('-', ' ').split())
    if not v:
        return None
    if v.startswith(('idea', 'research', 'propos', 'explor')):
        return 'ideas'
    if v.startswith(('crit', 'review', 'referee', 'check', 'verif')):
        return 'critic'
    if v.startswith(('lit', 'source', 'reading', 'library', 'citation')):
        return 'literature'
    return None


def pick_role(r, model, taken, want=None):
    """Roster role for a new slot of this model, given taken = {role: count of live slots of
    this model}. want (ideas / critic / literature): that role only -- it when one of its
    slots is free, else 'full' (the launch waits queued; a research launch never takes a
    critic slot). Without want: the first role with a free slot, 'full' when every slot of
    the model is taken. None if the model is not on the roster."""
    roles_of = [role for _, m, role in roster_slots(r) if m == model]
    if not roles_of:
        return None
    if want:
        return want if taken.get(want, 0) < roles_of.count(want) else 'full'
    left = dict(taken)
    for role in roles_of:
        if left.get(role, 0) > 0:
            left[role] -= 1
        else:
            return role
    return 'full'


def slot_brief(member_brief, swarm, model, role, root=None):
    """Brief for one roster slot: ideas -> member brief + ideas text; critic -> generic
    critic brief; literature -> member brief + literature rules."""
    if role == 'critic':
        from .post_types import POST_CONTRACT
        return CRITIC_BRIEF + '\n' + POST_CONTRACT + FOLLOW_UP
    text = ('Your job is the literature: find, read and cite sources relevant to the open claims and post what '
            'was searched and found, without mathematical inference.\n' + literature_text(swarm, model, root)) if role == 'literature' \
        else ideas_text(swarm, model, root)
    if role == 'ideas':
        from .proposer_controls import self_check_line
        check = self_check_line(swarm, model, root or ROOT, role='ideas')
        if check:member_brief = member_brief + '\n' + check
    from .post_types import POST_CONTRACT
    return member_brief + ('\n\nYour role:\n' + text if text else '') + ('\n' + POST_CONTRACT if POST_CONTRACT not in text else '') + FOLLOW_UP


def allowed(swarm, key, model, root=None):
    allow = model_list(swarm, key, root)
    return allow is None or str(model).lower() in allow


def brief_with_role(brief, swarm, model, root=None):
    text = role_text(swarm, model, root)
    from .proposer_controls import self_check_line
    check = self_check_line(swarm, model, root or ROOT)
    if check:
        brief = brief + '\n' + check
    if text:
        brief = brief + '\n\nYour role:\n' + text
    from .post_types import POST_CONTRACT
    return brief + ('\n' + POST_CONTRACT if POST_CONTRACT not in text else '') + FOLLOW_UP

# The stateless selector plans; coordinator alone assigns.
from .global_planner import PLANNER_BRIEF
