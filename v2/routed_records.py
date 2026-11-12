"""The full review record of each claim routed to a standing critic, as a file in its /work.

turn audit 2026-10-11: a routing message carries only each claim's id and first 160 characters, and
it is added to one forwarded request, so the client never stores it. Standing K3 and Flash critics of
problems 5, 6 and 1 then spent 56.9 of their 176.7 session-hours on board record reads, pages and
re-reads (19 h per run); of 2,748 records they read before a verdict, 1,024 were the routed claim,
its revisions, premises, literature or cited records. The record is written once per routed
revision as /work/routed/<record>.md (the certifier bundle without reviews: judge independently),
so one read_file gives it and a compaction loses nothing that cannot be re-read in one turn.
"""
import re
from pathlib import Path

ROUTED_DIR = 'routed'
_SAFE = re.compile(r'^\d{1,12}$')


def write_routed(work, items, claims, records):
    """items: [(record id, claim)]; claims: the claim ledger; records: board snapshot records.
    Returns the paths written (relative to work), skipping ids that are not plain integers."""
    from .cert_bundle import bundle, cited, included, BRIEF_BUDGET_BYTES
    out = []
    root = Path(work)/ROUTED_DIR
    for rid, claim in items:
        if not _SAFE.match(str(rid)) or not claim:
            continue
        text = bundle(claim, claims, records, reviews=None)
        shown = [r for r in records or () if (r.get('body') or {}).get('post_type') in ('reasoning', 'literature')]
        text += cited(claim, shown, exclude=included(claim, None), budget=max(0, BRIEF_BUDGET_BYTES - len(text.encode())))
        root.mkdir(exist_ok=True)
        path = root/('%s.md' % rid)
        tmp = path.with_name('.' + path.name + '.tmp')
        tmp.write_text(text); tmp.replace(path)
        out.append('/work/%s/%s.md' % (ROUTED_DIR, rid))
    return out


def routed_note(paths):
    if not paths:
        return ''
    return ('The full review record of each claim above (current text, earlier revisions, and the accepted claims and '
            'K3-checked literature it depends on; no reviews) is in %s. Read that file with read_file instead of reading '
            'these records from the board. K3-checked literature there may be relied on without re-fetching the source; '
            'post any doubt as an objection.' % ', '.join(paths))
