"""One evidence bundle for a bound review session (operator 2026-10-11: a certification takes a
handful of turns, not 10-96).

176 certifier and tie-break sessions of problems 1, 5 and 6 took a median 10.5 turns (max 55)
fetching what their brief lacked: 474 board reads, 1,131 file reads, 289 arXiv calls and 94
attempts to file a worker result. The release then added 12,000-byte review_page slices that
must all be read before an ACCEPT is admitted. The bundle is the claim's whole review record,
delivered in the session's first message without truncation (Fable recommendation 7):

- the current text of the bound revision;
- every earlier revision of the claim, oldest first;
- for certifiers and tie-breakers, every review posted on the claim (authors withheld; a
  tie-break's critiques also lose their verdict labels, as in its blinded design);
- the full text of every accepted claim it lists in Premise-refs;
- the full text and source card of every K3-checked literature report it lists in
  Literature-refs, marked as checked: the reviewer may rely on it without re-fetching, and
  posts any doubt as an objection.

Records the claim text cites by number (#N) follow as an optional appendix while the budget lasts,
so the reviewer need not read them from the board one at a time.

The harness delivers whole review pages in the brief up to BRIEF_BUDGET_BYTES and records them
as read (the delivery is the read receipt); pages past the budget still need review_page.
"""
import json

# A 64,000-token request shape compacts near 200 KB (v2/request_bound.compact_tokens); keep
# half of that for the reviewer's own reasoning and tool output.
BRIEF_BUDGET_BYTES = 96000

CHECKED_LITERATURE_NOTE = (
    'K3 has already checked each report in this section against the retained source bytes. You may '
    'rely on it as checked literature: do not re-search or re-fetch the source. If you doubt a '
    'report, post that doubt as an objection naming its record id.')

_ARXIV = __import__('re').compile(r'\b\d{4}\.\d{4,5}(?:v\d+)?\b')
_LABEL = __import__('re').compile(r'^\s*[*_#>]*\s*(status|verdict)\s*[*_]*\s*[:=].*$',
                                  __import__('re').I | __import__('re').M)


def _body(r):
    b = r.get('body')
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError:
            return {}
    return b if isinstance(b, dict) else {}


def _texts(records):
    """{record id: full text}, chunked posts reassembled."""
    from .claim_ledger import assemble
    out = {}
    for r in records or ():
        if isinstance(r, dict) and isinstance(r.get('id'), int):
            b = _body(r)
            out[r['id']] = str(b.get('statement') or b.get('text') or b.get('plan') or '')
    out.update({rid: text for rid, (_, text) in assemble(records).items()})
    return out


def _current(c):
    from .global_planner import current
    return current(c)


def bundle(claim, claims, records, *, reviews=None):
    """The bundle text. reviews: None (pair review: blinded, no reviews), 'labelled' (certifier)
    or 'blinded' (tie-break: verdict labels stripped)."""
    from .certification_checking import enabled, INDEPENDENT_LITERATURE
    literature_note = INDEPENDENT_LITERATURE if enabled() else CHECKED_LITERATURE_NOTE
    texts = _texts(records)
    by_id = {r['id']: r for r in records or () if isinstance(r, dict) and isinstance(r.get('id'), int)}
    rid = _current(claim)
    out = ['=== CLAIM UNDER REVIEW: record %s (current revision; full text) ===' % rid,
           str(claim.get('text') or texts.get(rid) or claim.get('statement') or '')]
    chain = [claim['records']['proposal']] + [r['record'] for r in claim.get('revisions') or []]
    earlier = [x for x in chain if x != rid and x is not None]
    if earlier:
        out += ['', '=== EARLIER REVISIONS OF THIS CLAIM (oldest first; superseded) ===']
        for n, x in enumerate(earlier):
            out += ['--- record %s (%s) ---' % (x, 'original proposal' if n == 0 else 'revision %d' % n), texts.get(x, '')]
    if reviews:
        ids = list(dict.fromkeys(list(claim['records'].get('verdicts') or []) +
                                 [i for i in (claim.get('verdict_records') or {}).values() if isinstance(i, int)]))
        out += ['', '=== REVIEWS POSTED ON THIS CLAIM (authors withheld; arguments, not authorities) ===']
        if not ids:
            out.append('(none)')
        for n, v in enumerate(sorted(ids), 1):
            b = _body(by_id.get(v, {}))
            target = b.get('re') if b.get('re') not in (None, '', '-') else '?'
            text = texts.get(v, '')
            if reviews == 'blinded':
                out += ['--- review %d (on record %s) ---' % (n, target), _LABEL.sub('', text).strip()]
            else:
                out += ['--- review %d: record %s, on record %s ---' % (n, v, target), text]
    current = {_current(c): c for c in (claims or {}).values()}
    premises = [i for i in claim.get('premise_refs') or [] if isinstance(i, int)]
    if premises:
        out += ['', '=== ACCEPTED CLAIMS THIS CLAIM DEPENDS ON (Premise-refs; full text) ===']
        for i in premises:
            c = current.get(i)
            state = c['state'] + (', certified' if c.get('certified') else '') if c else 'not a current claim revision'
            out += ['--- record %s (%s) ---' % (i, state), str((c or {}).get('text') or texts.get(i, ''))]
    literature = [i for i in claim.get('literature_refs') or [] if isinstance(i, int)]
    # K3-checked reports on arXiv sources the claim text cites without a Literature-refs link.
    cited_sources = {m.split('v')[0] for m in _ARXIV.findall(str(claim.get('text') or ''))}
    for c in sorted((claims or {}).values(), key=_current):
        if c.get('post_type') == 'literature' and c['state'] == 'accepted' and _current(c) not in literature:
            ids = {str(s.get('id', '')).split('v')[0] for s in ((c.get('literature') or {}).get('sources') or []) if isinstance(s, dict)}
            if ids & cited_sources:literature.append(_current(c))
    if literature:
        out += ['', '=== CHECKED LITERATURE THIS CLAIM DEPENDS ON OR CITES (Literature-refs; reports on cited arXiv sources) ===', literature_note]
        for i in literature:
            c = current.get(i)
            checked = bool(c) and c.get('post_type') == 'literature' and c['state'] == 'accepted'
            out += ['--- record %s (%s) ---' % (i, 'K3-checked literature' if checked else
                                                'NOT currently K3-checked: treat as unverified'),
                    str((c or {}).get('text') or texts.get(i, ''))]
            card = (c or {}).get('literature') or _body(by_id.get(i, {})).get('literature')
            if card:
                out.append('Source card: ' + json.dumps(card, sort_keys=True, ensure_ascii=False))
    out += ['', '=== END OF BUNDLE ===']
    return '\n'.join(out)


def brief_tail(scope):
    """The brief's evidence section for a bound scope returned by Board.bind_review."""
    n, k = scope['pages'], scope.get('delivered', 0)
    if k >= n:
        return ('\nThe complete evidence bundle follows (%d bytes). It is everything you need to judge the claim: '
                'do not page it with review_page or re-read these records from the board.\n\n%s'
                % (len(scope['delivered_text'].encode()), scope['delivered_text']))
    return ('\nEvidence bundle, pages 0-%d of %d follow. Read pages %d through %d with board_call review_page(page=N) '
            'before accepting.\n\n%s' % (k - 1, n, k, n - 1, scope['delivered_text']) if k else
            '\nRead EVERY board_call review_page(page=N), pages 0 through %d, before accepting.' % (n - 1))


_CITE = __import__('re').compile(r'(?:#|\brecords?\s+#?|\bboard:)(\d{1,9})\b', __import__('re').I)


def cited(claim, records, exclude=(), budget=0):
    """Optional appendix: the full text of records cited as #N in the claim text (reviews among
    them only when the session may see reviews), oldest first, while the byte budget lasts."""
    texts = _texts(records)
    ids = sorted({int(m.group(1)) for m in _CITE.finditer(str(claim.get('text') or ''))} - set(exclude))
    out, size, left, absent = [], 0, [], []
    for i in ids:
        if i not in texts:
            absent.append(i); continue  # e.g. an imported claim's numbers from an earlier session
        part = '--- record %s ---\n%s\n' % (i, texts[i])
        if size + len(part.encode()) > budget:
            left.append(i); continue
        out.append(part); size += len(part.encode())
    if not out and not left and not absent:
        return ''
    head = '\n=== RECORDS CITED IN THE CLAIM TEXT (context; not part of the claim) ===\n'
    tail = ('Not included for space: records %s (read_record if you need them).\n' % ', '.join(map(str, left))) if left else ''
    if absent:
        tail += ('Not on this board: %s. These numbers come from an earlier session or are not record ids; '
                 'they cannot be read here, so judge the claim from the text above.\n' % ', '.join('#%d' % i for i in absent))
    return head + ''.join(out) + tail


def included(claim, reviews):
    """Record ids the bundle itself already carries (excluded from the cited appendix)."""
    ids = {_current(claim), claim['records']['proposal']} | {r['record'] for r in claim.get('revisions') or []}
    ids |= {i for i in (claim.get('premise_refs') or []) + (claim.get('literature_refs') or []) if isinstance(i, int)}
    if reviews:
        ids |= set(claim['records'].get('verdicts') or []) | {i for i in (claim.get('verdict_records') or {}).values() if isinstance(i, int)}
    return ids
