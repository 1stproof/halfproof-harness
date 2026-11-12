"""Proof coverage of a final report (operator 2026-10-14: the writeups must contain the proofs).

For every result the report states as proved, is there a written proof, and how long is it next to
the certified argument it is bound to through PROVENANCE.md? No model is called; this is pattern
matching against the report, PROVENANCE.md and CLAIMS-MANIFEST.json. It never reads or judges the
mathematics: length against the run's own certified text is a proxy for "written out", not a check
that the proof is correct or complete.

A result is stated as proved when it is a theorem-like environment that is not in a literature,
unverified, conditional, conjectural or open section and whose title is not hedged ("Quoted from the
literature", "Conditional", ...). Its proof is the proof environment that follows it before the next
theorem-like environment or heading.
"""
import json
import re
from pathlib import Path

from .report_check import (HEDGE, LABEL, THEOREM_ENVS, _body, _headings, _context, _prov_refs,
                           _strip_comments)

# Sections whose theorem-like statements are not claims proved here.
NOT_PROVED_SECTION = re.compile(r"literature|unverified|(?<!un)conditional|conjectur|\bopen\b|remain|not\s+proved|"
                                r"background|prior\s+work|quoted|heuristic|failed|refuted", re.I)
NOT_PROVED_TITLE = re.compile(r"quoted|not\s+proved|proof\s+not\s+included|literature|conditional|conjectur|unverified|heuristic|\bcf\.|\bsee\b|"
                              r"\[[A-Za-z]+\d*\]|\\cite", re.I)
# Header lines of a board post ("Post-type: reasoning", "Task: ...") are not argument text.
POST_HEADER = re.compile(r"^(?:Post-type|Status|Task|Revision-of|Repair-of|Re|Premise-refs|Premises|Literature-refs|"
                         r"Kind|Type|Title|Author|Session|Lineage|Records?|Supersedes)\s*:.*$", re.I | re.M)
# A proof counts as written out with MIN_PROOF_WORDS words of its own, or WRITTEN_OUT of the certified argument's
# words when that is shorter. Calibration (frozen batch 3): certified texts carry review and source narration, so
# faithful proofs written by the run's writers run 4-25% of them (P09, judged close to acceptable by the operator,
# 6-10%); the ratio is reported, never required. Empty or placeholder proofs (P04: 0 words) fall below both.
WRITTEN_OUT = 0.3
MIN_PROOF_WORDS = 40


def _words(text):
    """Words of LaTeX or Markdown prose, maths tokens counted once per formula."""
    t = re.sub(r"\$\$.*?\$\$|\\\[.*?\\\]|\$[^$]*\$", ' M ', text, flags=re.S)
    t = re.sub(r"\\(?:begin|end)\{[^}]*\}|\\(?:label|cref|Cref|ref|eqref|cite)\{[^}]*\}", ' ', t)
    t = re.sub(r"\\[A-Za-z]+\*?", ' ', t)
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*|\d+|M", t))


def argument_words(claim):
    """Words of a certified claim's own text, board headers removed."""
    return _words(POST_HEADER.sub('', str(claim.get('text') or claim.get('statement') or '')))


def provenance_bindings(provenance):
    """{label: [record numbers in the order PROVENANCE.md lists them]} (one line, bullet or table
    row per label). The first number that is a certified claim's record is the label's own claim."""
    out = {}
    for piece in re.split(r"\n(?=\s*(?:[-*]|\||\d+\.)\s)|\n\s*\n", str(provenance or '')):
        labels = re.findall(r"(?<![\w])((?:thm|prop|lem|cor|claim|fact|def|rem|result|main)[:\-][\w:\-.]*\w)", piece)
        if not labels:
            continue
        refs = _prov_refs(piece)
        ordered = [int(x) for x in re.findall(r"(?<![\w.])(\d+)(?!\w|\.\d)", piece) if int(x) in refs]
        for lab in labels:
            seen = out.setdefault(lab, [])
            seen += [n for n in ordered if n not in seen]
    return out


def _record_index(manifest):
    """record number -> claim, for current records and every record a claim lists."""
    claims = manifest.get('claims', []) if isinstance(manifest, dict) else list(manifest or [])
    index = {}
    for c in claims:
        nums = {c.get('current_record')}
        recs = c.get('records') or {}
        if isinstance(recs, dict):
            nums.add(recs.get('proposal'))
        for n in nums:
            if isinstance(n, int):
                index.setdefault(n, c)
    return claims, index


def certified_reasoning(claim):
    return (claim.get('state') == 'accepted' and claim.get('post_type', 'reasoning') != 'literature'
            and not str(claim.get('text') or '').lstrip().lower().startswith('post-type: literature'))


def coverage(tex, provenance='', manifest=None):
    """Per-result proof coverage. Returns dict(results=[...], summary={...})."""
    tex = _strip_comments(str(tex or ''))
    body, _ = _body(tex)
    heads = _headings(body)
    env_re = re.compile(r"\\begin\{(%s)\*?\}(\s*\[(?:[^\[\]]|\[[^\]]*\])*\])?(.*?)\\end\{\1\*?\}" % '|'.join(THEOREM_ENVS), re.S)
    envs = list(env_re.finditer(body))
    stops = sorted([m.start() for m in envs] + [off for off, _, _ in heads] + [len(body)])
    bindings = provenance_bindings(provenance)
    claims, index = _record_index(manifest or {})
    results = []
    for m in envs:
        title = (m.group(2) or '').strip()[1:-1]
        context = _context(heads, m.start())
        lab = LABEL.search(m.group(3) or '') or LABEL.search(body[m.end():m.end() + 80].split('\n', 1)[0])
        label = lab.group(1) if lab else None
        hedged = bool(NOT_PROVED_TITLE.search(title) or any(NOT_PROVED_SECTION.search(h) for h in context)
                      or HEDGE.search(title))
        nxt = next((s for s in stops if s > m.end()), len(body))
        pm = re.search(r"\\begin\{proof\}(\[[^\]]*\])?(.*?)\\end\{proof\}", body[m.end():nxt], re.S)
        proof_words = _words(pm.group(2)) if pm else 0
        records = list(bindings.get(label, ())) if label else []
        bound = [index[r] for r in records if r in index]
        certified = [c for c in bound if certified_reasoning(c)]
        # The label's own claim: the first certified record PROVENANCE.md lists for it.
        arg = argument_words(certified[0]) if certified else 0
        ratio = (proof_words / arg) if arg else None
        written_out = bool(pm) and (proof_words >= MIN_PROOF_WORDS or (ratio is not None and ratio >= WRITTEN_OUT))
        results.append(dict(env=m.group(1), label=label, title=title, section=' > '.join(context),
                            stated_as_proved=not hedged, has_proof=bool(pm), proof_words=proof_words,
                            records=records, certified_records=[c.get('current_record') for c in certified],
                            argument_words=arg, ratio=round(ratio, 2) if ratio is not None else None,
                            written_out=written_out))
    proved = [r for r in results if r['stated_as_proved']]
    summary = dict(theorem_like=len(results), stated_as_proved=len(proved),
                   with_proof=sum(r['has_proof'] for r in proved),
                   written_out=sum(r['written_out'] for r in proved),
                   bound_to_certified=sum(bool(r['certified_records']) for r in proved),
                   certified_in_manifest=sum(certified_reasoning(c) for c in claims),
                   certified_used=len({n for r in proved for n in r['certified_records']}),
                   body_words=_words(body))
    return dict(results=results, summary=summary)


def coverage_dir(directory):
    """coverage() of FINAL.tex, PROVENANCE.md and CLAIMS-MANIFEST.json in one directory."""
    d = Path(directory)
    read = lambda n: (d / n).read_text(errors='replace') if (d / n).exists() else ''
    manifest = json.loads(read('CLAIMS-MANIFEST.json') or '{}')
    return coverage(read('FINAL.tex'), read('PROVENANCE.md'), manifest)


def gaps(result):
    """Results stated as proved whose proof is missing or not written out (for writer feedback)."""
    return [r for r in result['results'] if r['stated_as_proved'] and not r['written_out']]


if __name__ == '__main__':
    import sys
    for arg in sys.argv[1:]:
        r = coverage_dir(arg)
        print(arg, json.dumps(r['summary']))
        for row in r['results']:
            print('  ', json.dumps(row))
