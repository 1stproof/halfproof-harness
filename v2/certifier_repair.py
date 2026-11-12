"""Structural repair contract; all mathematical content comes from the reviewing model."""
import re
import json

MAX_REPAIR_DEPTH = 3

REPAIR_BRIEF = '''Judge the claim from the evidence bundle the harness gives you: it is the claim's full text and review record, not an excerpt. Read review_page only for pages the brief says it did not include. On REJECT or objection of a reasoning claim, give the objection (Objection: <exact failing step and why>) and a proposed repair in as much detail as possible. In the SAME board_post, after the objection, add a standalone line Proposed-repair: followed by:
Corrected-statement: <the full corrected statement, or the exact statement still needing proof>
Premise-refs: <JSON array of ALL current reasoning/source dependency record IDs, or [] for none>
Literature-refs: <optional JSON array of K3-confirmed current source report IDs>
Corrected-proof: <a complete corrected proof, if you can give one>
OR, when you cannot give a complete proof:
Exact-gap: <the precise unresolved step and why it is unresolved>
Repair-route: <the most specific route you can see, including obligations and checks; state explicitly if you see no route>
Do not shorten a proof to a sketch. The board atomically posts your proposal as a separate Post-type: reasoning, Revision-of the rejected revision, authored by you. It is unverified until fresh pair review and certification by the eligible certifier family, which excludes the family that wrote the current text. You never certify your own repair. A route with an unresolved gap is not a proved result. All content must be your own work inside this run; no operator or outside reviewer supplies mathematical direction. On ACCEPT do not include Proposed-repair. Automatic repairs are capped at three per lineage, including continuation imports and alternative branches. At depth three, post REJECT with the exact objection and no Proposed-repair; this terminal verdict refutes the claim. Verdict and repair remain atomic: if the session expires before posting, neither is committed and the prior claim remains unchanged. Use Exact-gap and Repair-route when time is short; post the complete atomic verdict as soon as ready within the existing session and campaign deadlines. There is no extra time allowance or verdict-first attachment phase.'''


def split(text):
    parts = re.split(r"^\s*Proposed-repair:\s*$", text, maxsplit=1, flags=re.M | re.I)
    return (parts[0].rstrip(), parts[1].strip()) if len(parts) == 2 else (text, None)


REPAIR_FORM = ('After your objection text, add these lines to the SAME post:\n'
               'Proposed-repair:\n'
               'Corrected-statement: <full corrected statement, or the exact statement still needing proof>\n'
               'Premise-refs: <JSON array of dependency record ids, or []>\n'
               'Corrected-proof: <complete corrected proof>\n'
               '(or, instead of Corrected-proof, both Exact-gap: <the unresolved step> and Repair-route: <the route, or "no route seen">)')


def validate(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('REJECT requires Proposed-repair: with Corrected-statement and Corrected-proof, or Exact-gap and Repair-route. ' + REPAIR_FORM)
    labels = {}
    matches = list(re.finditer(r"^\s*(Corrected-statement|Corrected-proof|Exact-gap|Repair-route|Premise-refs|Literature-refs):\s*", text, re.M | re.I))
    for i, m in enumerate(matches):
        key = m.group(1).lower()
        if key in labels: raise ValueError('duplicate repair field: ' + key)
        labels[key] = text[m.end():matches[i+1].start() if i+1 < len(matches) else len(text)].strip()
    if not labels.get('corrected-statement') or not (labels.get('corrected-proof') or (labels.get('exact-gap') and labels.get('repair-route'))):
        raise ValueError('repair requires a nonempty Corrected-statement and complete Corrected-proof, or both Exact-gap and Repair-route. ' + REPAIR_FORM)
    for key in ('premise-refs', 'literature-refs'):
        if key in labels:
            try:
                from .format_json import refs as parse_refs
                refs = parse_refs(labels[key])
            except (ValueError, TypeError, SyntaxError) as exc: raise ValueError(key + ' requires a JSON array of exact revision IDs') from exc
            if not isinstance(refs, list) or any(type(i) is not int or i <= 0 for i in refs):
                raise ValueError(key + ' requires a JSON array of exact revision IDs')
            labels[key] = refs
    return labels


def reviewer_family(worker, model=None):
    from .claim_ledger import MODEL_FAMILY
    model = str(model or '').lower()
    if not model:
        match = re.match(r'^critic:(?:cert|tiebreak)-(qwen24t|qwen|glm|k3|dsf|dsp)(?:[-:]|$)', str(worker))
        model = match.group(1) if match else ''
    return str(MODEL_FAMILY.get(model, model)).lower()


def author_family(c):
    return reviewer_family(c.get('revision_author') or c.get('proposer'),
                           c.get('revision_author_model') if c.get('revision_author') else c.get('proposer_model'))


def required_families(c):
    # Only the family that wrote the CURRENT text is excluded, so a lineage repaired by both
    # families stays certifiable. repair_author_families is provenance only; the depth cap
    # bounds repair loops.
    from .claim_ledger import load_policy
    count = load_policy('mixed-swarm').get('certification', {}).get('required_certifiers')
    author = author_family(c)
    if count is None:
        return [f for f in ('qwen', 'glm') if f != author]
    # Flash and Pro are one author family, while native verdict identities stay intact.
    exclude = 'deepseek' if author in ('dsf', 'dsp') else author
    return [f for f in ('qwen', 'glm', 'k3', 'dsp')
            if ('deepseek' if f == 'dsp' else f) != exclude][:count]


def lineage_brief(c):
    depth = c.get('repair_depth', 0)
    return ('Repair lineage depth: %s/%s. Automatic repair allowance exhausted: %s. '
            'Eligible certification families: %s.' %
            (depth, MAX_REPAIR_DEPTH, 'yes' if depth >= MAX_REPAIR_DEPTH else 'no',
             ', '.join(required_families(c)) or 'none (certification blocked)'))
