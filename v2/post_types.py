"""Declared post types; admission checks structure, K3 checks source scope and content.

No keyword classifier or mathematical correctness filter. Only bounded source evidence
is admissible on the literature path. A reviewer may return mixed content for splitting.
"""
import re

POST_CONTRACT = '''Declare Post-type: note, verdict, reasoning, or literature in the first eight lines.
Every claim must carry an explicit reasoning or literature post-type label. An unlabelled post that is not a verdict defaults to note.
A note or review verdict is never a result claim. Verdicts must carry Re: <exact revision record id>.
Result text replacing a plan or previous result must carry Revision-of: <exact current record id>; changed text needs fresh review.
With board_call post_finding pass revision_of=<exact current record id> as an argument; only a markdown post writes the Revision-of: line.
Reasoning claims keep pair review, blinded tie-break, selector, and, only when certification is on, both Qwen/GLM certification (for certifier-authored repairs, only the other family; never the author).
Literature is a Flash source report checked once by K3, reading the source itself; no tie-break or certification.
A literature post must start its report: "I was given the task to look for X in the literature, and this is what I found: ..."
Use board_call post_finding with post_type="literature", statement=<that report>, literature={"task": X,
"sources": [{"id": "arXiv id including vN", "sha256": "source_sha256 returned by arxiv_fetch", "location": "exact file/span", "quote": "exact quote"}],
"searches": [{"query": "exact query", "coverage": "files/versions searched", "result": "matches or no match"}],
"accesses": [{"url": "attempt URL", "at": "timestamp", "error": "access failure"}]} (accesses: failed attempts only; a successful read is a source).
Include at least one source, search, or access receipt. No mathematical conclusions, applicability judgments,
or inferences belong in a literature report. Put every inference in a separate Post-type: reasoning claim. Applying a literature result is a reasoning claim, not a literature post: state "result R from source S applies here in this way", cite the K3-confirmed literature post with literature_refs=[<exact revision record id>], and give the scope. It follows the full reasoning pipeline.
Mixed literature is returned to its author to split. For markdown literature posts include Literature: <the same JSON>.
A certifier or tie-break REJECT on reasoning must include Proposed-repair: with Corrected-statement and complete Corrected-proof, or Exact-gap and Repair-route. The board posts this atomically as its author's unverified reasoning Revision-of; it receives fresh pair review and other-family certification.
Use source LaTeX first, then arXiv HTML; share versioned source bytes, never conclusions or prior verdicts.'''


# The opening is checked by its fixed phrases, not by byte equality with literature.task: p02 (2026-10-11)
# lost every literature card to faithful paraphrases of the task. Leading label lines are tolerated.
OPENING = re.compile(r'(?:[ \t]*[A-Za-z][A-Za-z _-]{0,30}:[^\n]*\n){0,8}\s*I was given the task to look for\s+\S.*?'
                     r'\s+in the literature\s*[,.;:]?\s*(?:and\s+)?this is what I found\s*:', re.I | re.S)


# turn audit 2026-10-11: one message for any fault in any item ("each accesses item must contain exactly
# these non-empty string fields") left 45 of 100 rejected cards failing on current code; models deleted
# hashes or invented an error text to pass. Name the item, the missing/empty/extra fields and the rule.
ITEM_HELP = {
    'sources': 'A source is a file you read in this session: id = arXiv id with version (2411.13662v1), sha256 = the '
               'source_sha256 arxiv_fetch returned for it, location = file and line span, quote = exact text. A paper you '
               'could not read is not a source: record the attempt under accesses.',
    'searches': 'A search is query, coverage (files/versions searched), result (matches, or "no match").',
    'accesses': 'accesses lists only FAILED access attempts: url, at (timestamp), error (what failed). A successful read '
                'is a source, not an access; leave accesses [] when nothing failed.'}


def _item_fault(key, n, row, required):
    if not isinstance(row, dict):
        return ('literature schema: %s[%d] must be an object with exactly the fields %s; got %s. %s'
                % (key, n, ', '.join(sorted(required)), type(row).__name__, ITEM_HELP[key]))
    missing = sorted(required - set(row)); extra = sorted(map(str, set(row) - required))
    empty = sorted(k for k in required & set(row) if not isinstance(row[k], str) or not row[k].strip())
    if missing or extra or empty:
        return ('literature schema: %s[%d] must have exactly the non-empty string fields %s; missing: %s; empty: %s; '
                'not allowed: %s. %s' % (key, n, ', '.join(sorted(required)), ', '.join(missing) or 'none',
                                         ', '.join(empty) or 'none', ', '.join(extra) or 'none', ITEM_HELP[key]))
    return None


def validate(post_type, statement, literature=None):
    if post_type not in ('note', 'verdict', 'reasoning', 'literature'):
        raise ValueError('Post-type must be note, verdict, reasoning, or literature')
    if post_type != 'literature':
        return
    if not isinstance(literature, dict):
        raise ValueError('literature schema: literature must be a JSON object; pass literature={"task": "X", '
                         '"sources": [], "searches": [{"query": "exact query", "coverage": "files/versions searched", '
                         '"result": "matches or no match"}], "accesses": []} to post_finding, using your actual '
                         'task and evidence. For markdown posts include Literature: <the same JSON>. '
                         'This error checks metadata structure, not mathematical wording.')
    extra = set(literature) - {'task', 'sources', 'searches', 'accesses'}
    if extra:
        raise ValueError('literature schema: unsupported fields: ' + ', '.join(sorted(map(str, extra))) +
                         '; allowed fields are task, sources, searches, accesses. '
                         'Put reasoning in a separate Post-type: reasoning claim.')
    task = literature.get('task')
    if not isinstance(task, str) or not task.strip():
        raise ValueError('literature task required')
    if not OPENING.match(statement):
        raise ValueError('literature report must use the required task/report opening; for this card, start the '
                         'statement with: "I was given the task to look for ' + task + ' in the literature, and '
                         'this is what I found:" (the task may be worded in your own words)')
    count = 0
    for key, required in [('sources', {'id', 'sha256', 'location', 'quote'}),
                          ('searches', {'query', 'coverage', 'result'}),
                          ('accesses', {'url', 'at', 'error'})]:
        rows = literature.get(key, [])
        if not isinstance(rows, list):
            raise ValueError('literature schema: ' + key + ' must be a list of evidence objects')
        for n, row in enumerate(rows):
            fault = _item_fault(key, n, row, required)
            if fault:
                raise ValueError(fault)
            if key == 'sources' and not re.fullmatch(r'(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})v[1-9]\d*', row['id']):
                raise ValueError('literature schema: sources[%d].id %r must be an arXiv id with its version, e.g. 2411.13662v1. %s'
                                 % (n, row['id'][:60], ITEM_HELP['sources']))
            if key == 'sources' and not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
                raise ValueError('literature schema: sources[%d].sha256 must be the 64-hex-digit source_sha256 that arxiv_fetch '
                                 'returned for %s; got %r' % (n, row['id'], row['sha256'][:70]))
            count += 1
    if not count:
        raise ValueError('literature source/search/access evidence required')


def literature_state(claim):
    from .state_page import _family
    from .claim_ledger import is_cert, is_tiebreak, _slot_family
    checks = []
    for worker, verdict in claim['verdicts'].items():
        if is_cert(worker) or is_tiebreak(worker):
            continue
        family = _family(claim.get('verdict_models', {}).get(worker)) or _slot_family(worker, ['k3'])
        if family == 'k3':
            checks.append(verdict)
    # Any in-flight objection is retained, even after another check accepted.
    if any(v in ('reject', 'object') for v in checks):
        return 'needs-repair'
    return 'accepted' if 'accept' in checks else 'open'

POST_CONTRACT += " Every reasoning result must declare Premise-refs: [exact current revision IDs] for ALL source and reasoning dependencies, or Premise-refs: [] for none. Applicability is reasoning. Literature cards must quote retained source bytes with their exact archive SHA256 and file/line span; search absence stays bounded by actual coverage."
