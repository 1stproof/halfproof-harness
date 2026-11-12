"""Mechanical final-report check (operator 2026-10-07): runs on the writer's final report
before it is accepted. No model is called; everything is pattern matching against the
report text and the claim ledger (v2/claim_ledger.py).

Rule 1. A statement the report presents as proved/verified/certified/established/shown
(a theorem-like environment, or a sentence using one of those verbs) must correspond to a
ledger-accepted claim (writer_verified: k_accept distinct critics, no standing refutation
or objection). Otherwise it must be hedged as conditional/unverified, and a section
"Unverified results used" must exist.
Rule 2. A cited literature theorem used in the report must list its hypotheses, and an
accepted ledger claim must record a critic's acceptance that those hypotheses hold in the
problem's setting. Otherwise the report must state it as a result in a different setting,
not used as proof.

Rule 3 (operator 2026-10-11): record or claim numbers, board/ledger vocabulary and
hashes belong only in the separate PROVENANCE.md file. An embedded provenance section or
a missing companion file or detected board vocabulary blocks new reports. Archived
reports without the sidecar contract retain advisory vocabulary checks. A theorem is matched to its accepted
claim through that file (its \\label listed with the claim's record numbers) or by wording.
"Unverified results used" is required only when the report actually uses unverified or
conditional material, not whenever a hedge word such as "open" appears.

Permissive: hedging is recognised by vocabulary anywhere in the sentence, the theorem
title/body or the enclosing heading; the exact phrase "conditional on the unverified
results" is never required, and nothing is rejected on format alone. Feedback quotes only
the report's own words and the rule; it never carries mathematical content.
"""
import re

THEOREM_ENVS = ('theorem', 'proposition', 'lemma', 'corollary', 'claim', 'fact')
CLAIM_VERB = re.compile(r"\b(prov(?:ed|en|es)|verified|certified|established|establishes|shown|we\s+prove|we\s+show|"
                        r"demonstrated|settled|rigorous(?:ly)?)\b", re.I)
HEDGE = re.compile(r"(unverified|not\s+(?:yet\s+)?(?:been\s+)?(?:verified|proved|proven|established|certified|shown|accepted)|"
                   r"unprov|conjectur|(?<!un)conditional(?!\s+(?:expectation|probabilit|distribution|law|densit|variance))|heuristic|"
                   r"\bopen\b(?!\s+(?:set|subset|domain|ball|interval|neighbou?rhood|cover|map|region|cone))|refuted|disputed|unchecked|uncertified|unsourced|"
                   r"cannot\s+be|candidate|expected(?!\s+value)|plausib|\bsketch|\bwould\b|assuming|presumab|"
                   r"computation-checked|numeric|not\s+a\s+proof|tentative|speculat|\bclaimed\b|\bpurported|"
                   r"needs?[- ]repair|\bgap\b|withdrawn|retracted|orientation|in\s+progress|pending|(?<!\\)\bpartial)", re.I)
NEGATION = re.compile(r"(\bnot\b|\bno\b|\bnever\b|\bcannot\b|\bnothing\b|\bnowhere\b|\bnone\b|\bneither\b|\bnor\b|n't\b|\bfails?\b|\bfalse\b)", re.I)
_NEG = r"(?:\bnot\b|\bno\b|\bnever\b|\bcannot\b|\bnothing\b|\bnowhere\b|\bnone\b|\bneither\b|\bnor\b|n't\b)"
_VERB = r"(?:prov(?:e|ed|en|es)|verified|certified|established|shows?|shown|settled|demonstrated)"
NEGATED_CLAIM = re.compile(_NEG + r"(?:\W+\w+){0,14}?\W+" + _VERB + "|" + _VERB + r"(?:\W+\w+){0,4}?\W+(?:nowhere|by\s+no\b)|" + _VERB + r"(?:\W+(?:here|so\s+far|yet))?\W+(?:nothing|no\b|none)", re.I)
# Labelling conventions ("PROVED means ...", "the label proved is used for ...") define words, they assert nothing.
CONVENTION = re.compile(r"(?:''|\"|\}|\))\s*means\b|\b(?:label|term|word|tag|status)\b[^.]{0,40}?\bis\s+used\b|\bwe\s+(?:write|call)\b|\bthe\s+label\b", re.I)
THEOREM_USE = re.compile(r"\b(?:by|from|using|via|applying|invoking|according\s+to)\b[^.]{0,80}?\b(?:theorem|thm|lemma|proposition|corollary|result|bound|estimate)\b"
                         r"|\b(?:theorem|thm|lemma|proposition|corollary)\b[^.]{0,80}?\b(?:gives|yields|implies|shows|applies|provides)\b", re.I)
HEADING_HEDGE = re.compile(r"(unverified|conjectur|(?<!un)conditional(?!\s+(?:expectation|probabilit|distribution|law|densit|variance))|\bopen\b(?!\s+(?:sets?|subsets?|domains?|balls?|intervals?|maps?|covers?|cover))|remain|refuted|attempt|heuristic|not\s+established|"
                           r"\bgap|why\b|status|failed|rejected|withdrawn|limitations|caveat)", re.I)
UNVERIFIED_SECTION = re.compile(r"unverified|(?<!un)conditional", re.I)
# Material that obliges the "Unverified results used" section (not every hedge word does).
CONDITIONAL_USE = re.compile(r"(unverified|(?<!un)conditional(?!\s+(?:expectation|probabilit|distribution|law|densit|variance)))", re.I)
PROVENANCE = re.compile(r"provenance", re.I)
BOARD_TALK = re.compile(r"(verified\s+on\s+the\s+board|\bboard\s+(?:records?|posts?|ids?)\b|\bledger\b|claims-ledger|"
                        r"\bclaim\s+ids?\b|\brecords?\s*(?:\\#|#|no\.?)?\s*\d+|\\#\s*\d+|\bsha-?256\b|\b[0-9a-f]{40,64}\b)", re.I)
LABEL = re.compile(r"\\label(?:\[[^\]]*\])?\{([^}]*)\}")
# Reference and citation keys are names, not prose: \cref{sec-proved} asserts nothing.
SECTION_LABEL = re.compile(r"\\(?:sub)*section\*?(?:\[[^\]]*\])?\{[^}]*\}\s*\\label\{([^}]*)\}")
REF = re.compile(r"\\(?:[cC]ref|ref|eqref|autoref)\{([^}]*)\}")
CITATION = re.compile(r"(\\cite[a-zA-Z]*\s*(?:\[[^\]]*\]\s*)*\{[^}]*\}|arXiv[:\s]*\d{4}\.\d{4,5}|\bet\s+al\b|"
                      r"\bcited\s+(?:work|source|paper|theorem)|\bthe\s+source'?s?\b|\bpublished\b)", re.I)
LIT_USE = re.compile(r"(\b(?:theorem|thm|lemma|proposition|prop|corollary|cor)\b\.?|\bprove[sd]?\b|\bproven\b|\bestablish|\bshows?\b|"
                     r"\bobtain|\byield|\bgives?\b|\bupper\s+bound|\blower\s+bound|\brate\b|\bimplies\b|\bapplies\b|\bholds\b)", re.I)
DIFFERENT_SETTING = re.compile(r"(different\s+setting|other\s+setting|another\s+setting|own\s+setting|setting\s+of\s+the\s+source|"
                               r"not\s+(?:used|applied|invoked)\s+(?:here\s+)?as\s+(?:a\s+)?proof|not\s+(?:used\s+)?as\s+(?:a\s+)?proof|"
                               r"only\s+(?:as\s+|for\s+)?(?:attribution|context|orientation|comparison|background)|"
                               r"(?:attribution|context|background|comparison)\s+only|\(attribution\)|used\s+only\s+as|"
                               r"does\s+not\s+(?:apply|cover|transfer)|not\s+covered|not\s+applicable|outside\s+(?:the|our|its)\s+setting|"
                               r"differs?\s+from\s+(?:the|our|this)\s+(?:setting|problem|model|class)|in\s+(?:its|their)\s+(?:own\s+)?setting|"
                               r"not\s+the\s+(?:problem|model|class)\s+(?:posed|asked|here))", re.I)
HYPOTHESES = re.compile(r"(hypothes|assum|requir|provided\s+that|under\s+the\s+condition|conditions?\b|for\s+integer|"
                        r"\bwhenever\b|\bif\s+and\s+only\s+if\b|\bsuch\s+that\b|\bwith\s+\\?alpha\s*>|\blet\b|\bsuppose\b|\bfor\s+(?:all|every)\b)", re.I)
INAPPLICABLE = re.compile(r"(\bnot\b|n't\b|\bno\b|\bnone\b|\bneither\b|\bnor\b|\bfalse\b|\blacks?\b|\bfails?\b|inapplicab|violat|\bcannot\b|\bnever\b|\bwithout\b)", re.I)
SETTING_ACCEPT = re.compile(r"(hypothes|assumption|setting|applies|apply|transfer|covers?\b|class\b|model\b)", re.I)
RECORD_REF = re.compile(r"(?:records?|rec\.?|board[:\s]*|\#)\s*((?:\d+\s*(?:[,/]|and|--?|–)?\s*)+)", re.I)
STOP = set('that this with from have been were which their there these those then than into onto upon over under '
           'about also only when where while such each every some most more less same other between within without '
           'claim claims result results board record records proof proved proven state states'.split())


# ------------------------------------------------------------------ parsing

def _strip_comments(tex):
    return '\n'.join(re.sub(r'(?<!\\)%.*$', '', line) for line in tex.split('\n'))


def _body(tex):
    """(text, line offset): from \\begin{document} (or the start) to \\end{document}, the bibliography
    blanked out (line numbers kept) and appendices after it kept."""
    start = tex.find('\\begin{document}')
    start = 0 if start < 0 else start + len('\\begin{document}')
    end = tex.find('\\end{document}', start)
    text = tex[start:end if end >= 0 else len(tex)]
    blank = lambda m: re.sub(r'[^\n]', ' ', m.group())
    text = re.sub(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", blank, text, flags=re.S)
    text = re.sub(r"\\bibliography\{[^}]*\}", blank, text)
    return text, tex.count('\n', 0, start)


HEADING = re.compile(r"\\(section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
ENV = re.compile(r"\\begin\{(%s)\*?\}(\s*\[(?:[^\[\]]|\[[^\]]*\])*\])?(.*?)\\end\{\1\*?\}" % '|'.join(THEOREM_ENVS), re.S)
PROOF = re.compile(r"\\begin\{proof\}.*?\\end\{proof\}", re.S)
LEVEL = {'section': 0, 'subsection': 1, 'subsubsection': 2, 'paragraph': 3}


def _headings(body):
    """[(offset, level, title)] in order."""
    return [(m.start(), LEVEL[m.group(1)], m.group(2)) for m in HEADING.finditer(body)]


def _context(heads, pos):
    """Titles of the headings enclosing position pos (section > subsection > paragraph)."""
    stack = {}
    for off, lvl, title in heads:
        if off > pos:
            break
        stack[lvl] = title
        for k in [k for k in stack if k > lvl]:
            del stack[k]
    return [stack[k] for k in sorted(stack)]


def _plain(text):
    """Readable one-line excerpt of LaTeX."""
    t = re.sub(r"\\(?:label|ref|cref|Cref|eqref)\{[^}]*\}", '', text)
    t = re.sub(r"\\(?:begin|end)\{[a-z*]+\}", '', t)
    t = re.sub(r"\\(?:emph|textbf|textit|textsc|texttt)\{([^{}]*)\}", r"\1", t)
    return ' '.join(t.split())


SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\\(])|\n\s*\n|\\item\b")


def _words(text):
    return CITATION.sub(' ', REF.sub(' ', text))


def _navigation(text, proved_labels, section_labels):
    """Only a whole navigation sentence can bypass mathematical claim matching.

    References supply the objects of this closed grammar. Extra prose, equations,
    headings or clauses must go through the ordinary accepted-claim check.
    """
    cited = {key.strip() for m in REF.finditer(text) for key in m.group(1).split(',')}
    if not cited or not cited <= proved_labels | section_labels or not cited & proved_labels:
        return False
    def marker(match):
        keys = {k.strip() for k in match.group(1).split(',')}
        return ' THEOREMS ' if keys <= proved_labels else ' SECTIONS ' if keys <= section_labels else ' MIXED '
    prose = re.sub(r'\s+([,.])', r'\1', ' '.join(REF.sub(marker, text).replace('~', ' ').split()))
    return bool(re.fullmatch(
        r'(?:The (?:result|results|theorem|theorems)(?: proved (?:here|in this paper))? '
        r'(?:is|are) THEOREMS|We (?:prove|establish) THEOREMS|'
        r'THEOREMS (?:is|are) (?:proved|established)(?: (?:here|below))?)'
        r'(?:,? (?:proved )?in (?:Section|Sections) SECTIONS)?[.]?', prose, re.I))


def units(tex):
    """Statement units of the report: theorem-like environments and prose sentences.

    Each unit: dict(kind='env'|'sentence', env, title, text, line, headings, follow)."""
    tex = _strip_comments(tex)
    body, line0 = _body(tex)
    heads = _headings(body)
    out, spans = [], []
    # amsthm permits arbitrary environment identifiers; classify their printed names.
    custom = {m.group(1) for m in re.finditer(
        r"\\newtheorem\*?\{([^}]+)\}(?:\[[^\]]*\])?\{([^}]+)\}", tex)
        if re.search(r'\b(?:' + '|'.join(THEOREM_ENVS + ('result', 'observation', 'statement', 'assertion', 'bound', 'estimate')) + r')\b', m.group(2), re.I)
        and not HEDGE.search(m.group(2))}
    names = set(THEOREM_ENVS) | custom
    env_pattern = re.compile(r"\\begin\{(%s)\*?\}(\s*\[(?:[^\[\]]|\[[^\]]*\])*\])?(.*?)\\end\{\1\*?\}" %
                             '|'.join(re.escape(n) for n in sorted(names)), re.S)
    for m in env_pattern.finditer(body):
        follow = re.split(r"\n\s*\n|\\(?:sub)*section|\\paragraph", body[m.end():m.end() + 300], 1)[0]
        pm = re.match(r"\s*\\begin\{proof\}", follow)
        follow = '' if pm else follow.split('\\begin{', 1)[0]
        lab = LABEL.search(m.group(3) or '') or LABEL.search(body[m.end():m.end() + 80].split('\n', 1)[0])
        out.append(dict(kind='env', env=m.group(1), title=(m.group(2) or '').strip()[1:-1], text=m.group(3), label=lab.group(1) if lab else None,
                        line=line0 + body.count('\n', 0, m.start()) + 1, headings=_context(heads, m.start()), follow=follow,
                        start=m.start()))
        spans.append((m.start(), m.end()))
    spans += [(m.start(), m.end()) for m in PROOF.finditer(body)]
    spans.sort()
    # Prose: everything outside environments and proofs, minus displayed math and headings.
    pos, pieces = 0, []
    for a, b in spans:
        if a >= pos:
            pieces.append((pos, body[pos:a]))
            pos = b
    pieces.append((pos, body[pos:]))
    for off, chunk in pieces:
        masked = re.sub(r"\\begin\{(equation|align|gather|multline|displaymath)\*?\}.*?\\end\{\1\*?\}",
                        lambda x: ' ' * len(x.group()), chunk, flags=re.S)
        masked = HEADING.sub(lambda x: ' ' * len(x.group()), masked)
        cursor = 0
        for part in SENT_SPLIT.split(masked):
            if part is None:
                continue
            i = masked.find(part, cursor)
            cursor = i + len(part) if i >= 0 else cursor
            s = part.strip()
            if len(s) < 12:
                continue
            at = off + (i if i >= 0 else 0) + (len(part) - len(part.lstrip()))
            out.append(dict(kind='sentence', env=None, title='', text=s, line=line0 + body.count('\n', 0, at) + 1,
                            headings=_context(heads, at), follow='', start=at))
    for off, lvl, title in heads:
        out.append(dict(kind='heading', env=None, title='', text=title, line=line0 + body.count('\n', 0, off) + 1,
                        headings=_context(heads, off)[:-1], follow='', start=off))
    out.sort(key=lambda u: u['start'])
    return out


# ------------------------------------------------------------------ ledger side

def _tokens(text):
    return {w for w in re.findall(r"[a-z][a-z0-9-]{3,}", _plain(str(text)).lower()) if w not in STOP}


def accepted_claims(claims, policy=None):
    """Ledger-accepted claims (claim_ledger.writer_verified). claims: the ledger() dict, a list of
    such claim dicts, or rows parsed by parse_ledger_markdown."""
    from .claim_ledger import writer_verified
    rows = list(claims.values()) if isinstance(claims, dict) else list(claims or ())
    # Rows read from CLAIMS-LEDGER.md carry the state the harness computed under the run's policy.
    return [c for c in rows if isinstance(c, dict) and c.get('post_type') != 'literature' and (c.get('state') == 'accepted' if c.get('from_markdown')
                                                         else writer_verified(c, policy))]


def parse_ledger_markdown(text):
    """CLAIMS-LEDGER.md (as written for writers) -> claim rows with state/accepts/records.
    A row's state 'accepted' in that file already means writer-verified."""
    rows = []
    for line in str(text or '').splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        post_type = cells.pop(1) if len(cells) == 7 else 'reasoning'
        if len(cells) != 6 or cells[1] in ('State', '---', '-') or set(cells[1]) <= set('-'):
            continue
        try:
            acc, rej = int(cells[2]), int(cells[3])
        except ValueError:
            continue
        prop = int(cells[4]) if cells[4].isdigit() else None
        rows.append(dict(from_markdown=True, post_type=post_type, key=cells[0].replace('\\|', '|'), statement=cells[0].replace('\\|', '|'), state=cells[1],
                         accepts=acc if cells[1] == 'accepted' else 0, rejects=rej,
                         records=dict(proposal=prop, verdicts=[int(x) for x in re.findall(r"\d+", cells[5])])))
    return rows


def _claim_text(c, texts):
    prop = (c.get('records') or {}).get('proposal')
    return ' '.join(str(x) for x in (c.get('statement'), c.get('key'), c.get('text') or (texts or {}).get(prop)) if x)


# turn audit 2026-10-11: PROVENANCE.md bindings written "accepted claim $151$", "current_record: 37",
# "Record: 37" or as a table row were read as no number at all (p05 closing-v2: 14 of 16 feedback
# items, then the fallback report was delivered). The writer is taught "current_record number", so
# those forms bind; a label PROVENANCE.md mentions without a readable number gets a precise message.
PROV_REF = re.compile(r"(?:current\s*-?\s*record|records?|rec\.?|board|claims?|\#)\s*(?:no\.?|number|id)?\s*[:=]?\s*\$?\s*"
                      r"((?:\d+\s*\$?\s*(?:[,/]|and|--?|–)?\s*)+)", re.I)


def _prov_refs(piece):
    """Record numbers a PROVENANCE.md paragraph or table row binds."""
    out = {int(x) for m in PROV_REF.finditer(piece) for x in re.findall(r"\d+", m.group(1))}
    for row in piece.split('\n'):
        if row.count('|') >= 2:  # a Markdown table row: bare integer cells are record numbers
            out |= {int(c.strip(' $')) for c in row.strip().strip('|').split('|') if re.fullmatch(r"\s*\$?\d+\$?\s*", c)}
    return out


def _refs(text):
    out = set()
    for m in RECORD_REF.finditer(text):
        out |= {int(x) for x in re.findall(r"\d+", m.group(1))}
    return out


def _matches(unit_text, accepted, texts, extra_refs=()):
    """Accepted claims this unit corresponds to: by record number (in the text or listed for its
    label in PROVENANCE.md), or by most of the claim's words."""
    refs, toks = _refs(unit_text) | set(extra_refs), _tokens(unit_text)
    hits = []
    for c in accepted:
        recs = c.get('records') or {}
        ids = {recs.get('proposal')} | set(recs.get('verdicts') or ())
        ct = _tokens(_claim_text(c, texts))
        if c.get('claim_id'):
            # Exact content, or a PROVENANCE.md binding to the current record (below),
            # authorizes; restatements are judged by the independent final semantic audit.
            from .claim_ledger import _content
            content = _content(_plain(LABEL.sub('', unit_text)))
            from .claim_ledger import _fields
            candidates = [c.get('statement'), c.get('text'), _fields(c.get('text')).get('claim')]
            candidates += [s['quote'] for s in (c.get('literature') or {}).get('sources',[])]
            candidates += str(c.get('text') or '').split('\n\n')
            if any(content and content == _content(_plain(x or '')) for x in candidates):
                hits.append(c)
            elif set(extra_refs) & {c.get('current_record'), recs.get('proposal')} - {None}:
                # A PROVENANCE.md binding of this label to the claim's CURRENT record also
                # authorizes it here; faithfulness of the restatement is decided by the
                # mandatory independent semantic audit at closing (final_checks), not by bytes.
                hits.append(c)
            continue
        overlap = len(ct & toks)
        if (refs & {i for i in ids if isinstance(i, int)} and ct and overlap >= min(2, len(ct))) or (len(ct) >= 3 and overlap >= 0.6 * len(ct)):
            hits.append(c)
    return hits


def _cite_keys(text):
    keys = set()
    for m in re.finditer(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}", text):
        keys |= {k.strip().lower() for k in m.group(1).split(',') if k.strip()}
    keys |= {m.group(1) for m in re.finditer(r"(\d{4}\.\d{4,5})", text)}
    keys |= {w.lower() for w in re.findall(r"\b([A-Z][a-z]{3,})(?=\s*(?:--|–|-|,|\s+et\s+al|\s+and\s+[A-Z]|\s*\\cite|\s*\[))", text)}
    return keys


def _lit_accepted(unit_text, accepted, texts):
    """An accepted claim naming the same source and speaking to hypotheses/setting."""
    keys = _cite_keys(unit_text)
    for c in accepted:
        ct = _claim_text(c, texts)
        low = ct.lower()
        if keys and any(k in low for k in keys) and SETTING_ACCEPT.search(ct) and not INAPPLICABLE.search(ct):
            return c
    return None


# ------------------------------------------------------------------ the check

def _hedged(text):
    return bool(HEDGE.search(text))


def _provenance(heads, body):
    """(spans, {label: record ids}) of the Provenance appendix: each line or item there that
    names a \\ref and record numbers binds those numbers to the label."""
    spans, labels = [], {}
    for i, (off, lvl, title) in enumerate(heads):
        if not PROVENANCE.search(title):
            continue
        end = next((o for o, l, _ in heads[i + 1:] if l <= lvl), len(body))
        spans.append((off, end))
        part = body[off:end]
        for piece in re.split(r"\\item\b|\n\s*\n", part) if '\\item' in part else part.split('\n'):
            ids = _refs(piece)
            for m in REF.finditer(piece):
                for lab in m.group(1).split(','):
                    labels.setdefault(lab.strip(), set()).update(ids)
    return spans, labels


def check(tex, claims=(), *, policy=None, texts=None, provenance=None):
    """Run both rules on the report. claims: ledger() output or parsed CLAIMS-LEDGER.md rows.
    texts: optional {proposal record id: full claim text} for better matching.
    Returns dict(ok, failures=[...], counts)."""
    accepted = accepted_claims(claims, policy)
    rows = list(claims.values()) if isinstance(claims, dict) else list(claims)
    literature = [c for c in rows if c.get('post_type') == 'literature' and c.get('state') in ('accepted', 'source-checked')]
    failures, style, hedged_seen = [], [], False
    body, line0 = _body(_strip_comments(tex))
    prov_spans, prov = _provenance(_headings(body), body)
    if provenance is not None:
        if not provenance.strip():
            failures.append(dict(rule='missing-provenance',line=None,excerpt='',missing='save a nonempty PROVENANCE.md alongside FINAL.tex'))
        # External provenance replaces legacy in-paper mappings.
        prov = {}
        # One Markdown paragraph per mapping; exact labels, no fuzzy label matching.
        pieces = [(p, p) for p in re.split(r'\n\s*\n', provenance)]
        md_heads = list(re.finditer(r'^#{1,6}\s+([^\n]+)', provenance, re.M))
        for i, heading in enumerate(md_heads):
            end = md_heads[i+1].start() if i+1 < len(md_heads) else len(provenance)
            pieces.append((heading.group(1), provenance[heading.end():end]))
        for label_text, piece in pieces:
            ids = _prov_refs(re.sub(r'[`*_]', '', piece))
            for lab in LABEL.findall(tex):
                if re.search(r'(?<![\w:.-])' + re.escape(lab) + r'(?![\w.-]|:[\w.-])', label_text):  # "thm:main: record 37" names thm:main
                    prov.setdefault(lab, set()).update(ids)
        if prov_spans:
            failures.append(dict(rule='provenance-in-paper', line=None, excerpt='Provenance appendix',
                              missing='move provenance to PROVENANCE.md'))
    in_prov = lambda u: any(a <= u['start'] < b for a, b in prov_spans)
    all_units = units(tex)
    proved_labels = set()  # theorem labels that correspond to an accepted claim
    section_labels = set(SECTION_LABEL.findall(body))
    for u in all_units:
        if u['kind'] == 'env' and u.get('label') and not _hedged(u['title'] + ' ' + u['text']):
            own = LABEL.sub('', u['title'] + ' ' + u['text'])
            if _matches(own, accepted, texts, prov.get(u['label'], ())):
                proved_labels.add(u['label'])
    for m in PROOF.finditer(body):  # proofs are not statement units, but Rule 3 reaches them
        if not any(a <= m.start() < b for a, b in prov_spans):
            for t in BOARD_TALK.finditer(m.group()):
                style.append(dict(rule='record-ids-in-body', line=line0 + body.count('\n', 0, m.start() + t.start()) + 1,
                                  excerpt=_plain(m.group()[max(0, t.start() - 100):t.end() + 100])[:220],
                                  missing='record or claim numbers, board/ledger vocabulary and hashes belong only in PROVENANCE.md'))
                break
    for u in all_units:
        if in_prov(u):
            continue
        if BOARD_TALK.search(u['title'] + ' ' + u['text']):
            style.append(dict(rule='record-ids-in-body', line=u['line'], excerpt=_plain(u['title'] + ' ' + u['text'])[:220],
                              missing='record or claim numbers, board/ledger vocabulary and hashes belong only in PROVENANCE.md'))
        if u['kind'] == 'heading' and u['text'].lower() in ('results proved here', 'results quoted from the literature'):
            continue
        heads = ' '.join(u['headings'])
        head_hedge = bool(HEADING_HEDGE.search(heads))
        own = LABEL.sub('', u['title'] + ' ' + u['text'])
        excerpt = _plain(u['title'] + (': ' if u['title'] else '') + u['text'])[:220]
        # An uncited sentence under that heading quotes nothing; the ordinary checks below
        # still bind any result it asserts to an accepted claim or a theorem proved here.
        quoted = 'results quoted from the literature' in heads.lower() and (
            u['kind'] == 'env' or bool(CITATION.search(own)))
        refs = prov.get(u.get('label'), ())
        lit_match = _matches(own, literature, texts, refs)
        if u['kind'] != 'heading' and quoted:
            if (u['kind'] == 'env' or CLAIM_VERB.search(_words(own)) and not NEGATED_CLAIM.search(own) and not _hedged(own)) and not lit_match:
                failures.append(dict(rule='literature-label', line=u['line'], excerpt=excerpt,
                                     missing='quoted result needs a source-checked literature claim'))
            continue
        if u['kind'] == 'env' and lit_match and not _matches(own, accepted, texts, refs):
            failures.append(dict(rule='literature-label', line=u['line'], excerpt=excerpt,
                                 missing='put source results in Results quoted from the literature, apart from Results proved here'))
            continue
        lit_zone = own + ' ' + u['follow']
        is_lit = bool(CITATION.search(lit_zone)) and (u['kind'] == 'env' or LIT_USE.search(own))
        if u['kind'] in ('sentence', 'heading'):
            asserted = bool(CLAIM_VERB.search(_words(own)))
            soft = _hedged(own) or bool(NEGATED_CLAIM.search(own)) or (u['kind'] == 'sentence' and bool(CONVENTION.search(own)))
        else:
            asserted = True
            soft = _hedged(own)
        if CONDITIONAL_USE.search(own) and not NEGATED_CLAIM.search(own):
            hedged_seen = True
        used = asserted or bool(THEOREM_USE.search(own))
        if u['kind'] == 'heading':
            is_lit = False
        if is_lit and (used or u['kind'] == 'env'):
            # Rule 2: a cited result used in the report.
            if DIFFERENT_SETTING.search(lit_zone) or DIFFERENT_SETTING.search(heads):
                continue
            if u['kind'] == 'sentence' and (NEGATED_CLAIM.search(own) or (NEGATION.search(own) and not asserted)
                                            or head_hedge or not used):
                continue  # a sentence that only reports, quotes or negates the literature
            has_h = bool(HYPOTHESES.search(own))
            ok = _lit_accepted(lit_zone, accepted, texts)
            if has_h and ok:
                continue
            failures.append(dict(rule='literature-setting', line=u['line'], excerpt=excerpt,
                                 missing=('hypotheses not listed; ' if not has_h else '') +
                                 'no accepted critic check that the hypotheses hold in this problem\'s setting'))
            continue
        if not asserted or soft or head_hedge:
            continue
        if u['kind'] == 'env' and u.get('label') in proved_labels:
            continue
        lab = LABEL.search(u['text']) if u['kind'] == 'sentence' else None
        if lab and _matches(own, accepted, texts, prov.get(lab.group(1), ())):
            continue  # a labelled item bound to its accepted claim in PROVENANCE.md
        manifest = any(c.get('claim_id') for c in rows)
        # Under a claims manifest only theorem labels can carry a navigation sentence, never sections.
        if u['kind'] == 'sentence' and _navigation(own, proved_labels, set() if manifest else section_labels):
            continue  # references without an additional mathematical assertion
        if _matches(own, accepted, texts):
            continue
        label = u.get('label') if u['kind'] == 'env' else (lab.group(1) if lab else None)
        failures.append(dict(rule='unverified-as-proved', line=u['line'], excerpt=excerpt,
                             missing=_binding_gap(label, prov, accepted, provenance is not None)))
    has_section = any(UNVERIFIED_SECTION.search(t) for _, _, t in _headings(body))
    if hedged_seen and not has_section and not re.search(r"unverified results used", body, re.I):
        failures.append(dict(rule='missing-unverified-section', line=None, excerpt='',
                             missing='unverified or conditional material is used but no "Unverified results used" section lists it'))
    if provenance is not None and any(c.get('claim_id') and c.get('text') for c in rows):
        # Rule 4 (operator 2026-10-14): a result stated as proved carries its certified proof, written out.
        from .proof_coverage import coverage, gaps, WRITTEN_OUT
        for r in gaps(coverage(tex, provenance, {'claims': rows})):
            if not r['certified_records']:
                continue  # unbound statements are Rule 1's business
            failures.append(dict(rule='missing-proof', line=None, excerpt=r['label'],
                                 missing=('no proof follows it' if not r['has_proof'] else
                                          'its proof has %d words against %d in the certified argument of record %s (at least %d%% required)'
                                          % (r['proof_words'], r['argument_words'], r['certified_records'][0], round(100 * WRITTEN_OUT)))
                                 + '; write out the certified proof of record %s from CERTIFIED-PROOFS.md in full, or move the statement to '
                                   '"Unverified results used" marked unproved' % r['certified_records'][0]))
    if provenance is not None:
        failures.extend(style)
        style = []
    counts = {}
    for f in failures:
        counts[f['rule']] = counts.get(f['rule'], 0) + 1
    for f in style:
        counts[f['rule']] = counts.get(f['rule'], 0) + 1
    return dict(ok=not failures, failures=failures, style=style, counts=counts, accepted_claims=len(accepted))


def _binding_gap(label, prov, accepted, external):
    """The missing-text of a Rule 1 item: when the unit has a \\label, what PROVENANCE.md must say."""
    base = 'no ledger-accepted claim corresponds to this statement'
    if not label or not external:
        return base
    current = sorted({c.get('current_record') or (c.get('records') or {}).get('proposal') for c in accepted} - {None})
    form = 'write the line "%s: record N" in PROVENANCE.md, with N the current_record of the accepted claim it states' % label
    if label not in prov:
        return '%s; \\label{%s} is not listed in PROVENANCE.md: %s' % (base, label, form)
    if not prov[label]:
        return '%s; PROVENANCE.md mentions %s but no record number was recognised next to it: %s' % (base, label, form)
    return ('%s; PROVENANCE.md binds %s to record %s, which is not the current record of an accepted claim (current records: %s)'
            % (base, label, ', '.join(map(str, sorted(prov[label]))), ', '.join(map(str, current)) or 'none'))


# ------------------------------------------------------------------ writer loop

FEEDBACK_HEAD = ('[HARNESS: final-report check] The report was not accepted. Each item below quotes your report '
                 'and names the rule it does not meet. Fix every item and save FINAL.tex again.\n'
                 'Rule 1: a statement presented as proved, verified, certified, established or shown must be a claim '
                 'listed as accepted in CLAIMS-LEDGER.md; otherwise state it explicitly as "conditional on the unverified '
                 'results" and list it under a section titled "Unverified results used", or remove it. A theorem matches its '
                 'accepted claim when PROVENANCE.md lists its \\label with that claim\'s record number. Use \\cref '
                 'for navigation to accepted theorems; additional mathematical assertions must themselves '
                 'correspond to accepted claims.\n'
                 'Rule 2: a cited literature theorem that the report uses must list its hypotheses, and CLAIMS-LEDGER.md '
                 'must show a critic accepted that they hold in this problem\'s setting; otherwise state it as a result '
                 'in a different setting, not used as proof.\n'
                 'Rule 3: record or claim numbers, board or ledger vocabulary and hashes appear only in PROVENANCE.md; '
                 'there, list each theorem by its exact LaTeX label with the session and record numbers of its accepted claim. '
                 'The body states and proves the mathematics itself.\n'
                 'Rule 4: every result stated as proved is followed by its proof, written out in full from the certified '
                 'argument in CERTIFIED-PROOFS.md; a statement whose proof cannot be written out is moved to "Unverified '
                 'results used" and marked unproved.\n')
RULE_NAME = {'unverified-as-proved': 'Rule 1', 'literature-setting': 'Rule 2', 'missing-unverified-section': 'Rule 1',
             'record-ids-in-body': 'Rule 3', 'provenance-in-paper': 'Rule 3', 'missing-provenance': 'Rule 3',
             'missing-proof': 'Rule 4'}


def feedback(result, limit=40):
    """Generic failure list for the writer: the report's own words, the rule, no mathematics."""
    lines = [FEEDBACK_HEAD]
    items = list(result['failures']) + list(result.get('style') or ())
    for i, f in enumerate(items[:limit], 1):
        where = 'line %d' % f['line'] if f.get('line') else 'whole report'
        lines.append('%d. %s, %s: "%s" -- %s.' % (i, RULE_NAME.get(f['rule'], f['rule']), where, f['excerpt'], f['missing']))
    if len(items) > limit:
        lines.append('... and %d more of the same kinds.' % (len(items) - limit))
    return '\n'.join(lines) + '\n'


UNICODE_TEX = {'≤': r'$\le$', '≥': r'$\ge$', '≠': r'$\ne$', '≈': r'$\approx$', '≍': r'$\asymp$', '∞': r'$\infty$',
               '→': r'$\to$', '∈': r'$\in$', '⊂': r'$\subset$', '×': r'$\times$', '·': r'$\cdot$', '±': r'$\pm$',
               '—': '---', '–': '--', '‘': "`", '’': "'", '“': "``", '”': "''", '…': '...',
               **{c: r'$\%s$' % n for c, n in zip('αβγδεζηθκλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ',
               'alpha beta gamma delta varepsilon zeta eta theta kappa lambda mu nu xi pi rho sigma tau phi chi psi omega '
               'Gamma Delta Theta Lambda Xi Pi Sigma Phi Psi Omega'.split())}}


def _tex_escape(text, limit=4000):
    """Plain text -> LaTeX that compiles with pdflatex: specials escaped, common Unicode mathematics
    mapped, any other non-ASCII character written as [U+XXXX]. Truncated before escaping."""
    text = ' '.join(str(text).split())[:limit]
    out = []
    for ch in text:
        if ch in '\\&%$#_{}~^':
            out.append({'\\': r'\textbackslash{}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}.get(ch, '\\' + ch))
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append(UNICODE_TEX.get(ch, '[U+%04X]' % ord(ch)))
    return ''.join(out)


def fallback_report(claims, policy=None, *, texts=None, title='Solution to Problem N'):
    """A report built only from ledger-accepted claims (used after the bounded tries)."""
    # A recovery document cannot manufacture proofs from acceptance metadata.
    # Keep the acceptance inventory in the companion file, not the paper.
    return '\n'.join([r'\documentclass[12pt]{article}', r'\usepackage{fullpage}',
        r'\begin{document}', r'\title{%s}' % _tex_escape(title), r'\author{}',
        r'\date{}', r'\maketitle', r'\section{Status}',
        'The problem is not solved unconditionally by this report. No complete proof is supplied.',
        r'\section{Results quoted from the literature}', 'No literature result is quoted.',
        r'\section{Results proved here}', 'No result is proved in this document.',
        r'\section{What remains open}', 'The original question remains unresolved in this document.',
        r'\section{Unverified results used}', 'None: no unverified result is used.',
        r'\end{document}', ''])


def fallback_provenance(claims, policy=None, *, texts=None):
    lines = ['# Recovery report provenance', '',
             'No accepted proof was reproduced in the recovery paper; no mathematical result is claimed there.', '']
    for c in accepted_claims(claims, policy):
        rid = (c.get('records') or {}).get('proposal')
        body = c.get('text') or (texts or {}).get(rid) or c.get('statement') or c.get('key')
        lines += [f"Session {c.get('session', 'current')}; proposal record {rid}; "
                  f"verdict records {(c.get('records') or {}).get('verdicts', [])}; "
                  f"{c.get('accepts', 0)} critic accepts. Accepted inventory only: {body}", '']
    return '\n'.join(lines)


def gate(report, claims, revise, *, max_tries=2, policy=None, texts=None, title='Solution to Problem N'):
    """The writer loop. revise(feedback_text) -> new report text (or None when the writer
    produced nothing). After max_tries failed revisions, the fallback report is returned.
    Returns dict(outcome='accepted'|'fallback', report, tries, results)."""
    results = []
    for attempt in range(max_tries + 1):
        res = check(report, claims, policy=policy, texts=texts) if report else \
            dict(ok=False, failures=[dict(rule='missing-report', line=None, excerpt='', missing='no report')], counts={})
        results.append(res)
        if res['ok']:
            return dict(outcome='accepted', report=report, tries=attempt, results=results)
        if attempt == max_tries:
            break
        new = revise(feedback(res))
        report = new if new else report
    return dict(outcome='fallback', report=fallback_report(claims, policy, texts=texts, title=title),
                tries=max_tries, results=results)


RECORD_HEAD = re.compile(r"^## (.*)\(record (\d+)\)[^\n]*$", re.M)


def record_texts(directory):
    """{record id: text} for claim matching from the writer directory: the research record of earlier
    sessions (PRIOR-WORK-NNN.md, one '## ... (record N)' section per post, a board post preferred
    over a later section about it) or, without it, BOARD.json. Not both: record numbers repeat across
    sessions."""
    import json as _json
    from pathlib import Path as _Path
    d, out = _Path(directory), {}
    if (d / 'CLAIMS-MANIFEST.json').exists():
        try:
            from .claim_manifest import read
            return {c['current_record']: c['text'] for c in read(d)}
        except (OSError, ValueError, KeyError, TypeError):
            return {}
    parts = sorted(d.glob('PRIOR-WORK-[0-9]*.md'))
    if parts:
        for part in parts:
            try:
                text = part.read_text(errors='replace')
            except OSError:
                continue
            heads = list(RECORD_HEAD.finditer(text))
            for i, m in enumerate(heads):
                rid = int(m.group(2)); end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
                if rid not in out or (m.group(1).startswith('Board') and not out[rid][0]):
                    out[rid] = (m.group(1).startswith('Board'), text[m.end():end])
        return {k: v[1] for k, v in out.items()}
    try:
        return _parse_texts(_json.loads((d / 'BOARD.json').read_text(errors='replace')).get('records'))
    except (OSError, ValueError, AttributeError):
        return {}


def _parse_texts(records):
    """{record id: statement text} from board records (proposal texts for claim matching)."""
    import json as _json
    out = {}
    for r in records or ():
        if not isinstance(r, dict) or not isinstance(r.get('id'), int):
            continue
        body = r.get('body')
        if isinstance(body, str):
            try:
                body = _json.loads(body)
            except ValueError:
                body = {'text': body}
        if isinstance(body, dict):
            t = body.get('statement') or body.get('text')
            if isinstance(t, str):
                out[r['id']] = t
    return out
