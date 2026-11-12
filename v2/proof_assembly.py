"""Certified proofs in the final report (operator 2026-10-14: the writeups must contain the PROOFS).

The writer may state as proved only certified claims, and each must carry its certified proof. This
module supplies both halves without any model call:

* certified_proofs_md(manifest): CERTIFIED-PROOFS.md for the writer's directory, every certified
  reasoning claim with its full certified text inline, premises first, so the writer transcribes
  proofs instead of paging evidence files at 20,000 characters per read.
* complete_proofs(tex, provenance, manifest): after writing, every result the report states as
  proved that is bound (PROVENANCE.md) to a certified claim and lacks a written-out proof receives
  that claim's certified text as its proof; a result stated as proved with no certified binding is
  retitled as not proved and its argument relabelled as unverified. Nothing the writer wrote is
  deleted, and no wording is chosen by the harness beyond fixed labels.
* assembled_report(manifest): when no model draft exists, a document made of the certified claims
  and their certified texts, instead of the administrative placeholder that proves nothing.

The certified text is the run's own accepted argument, transcribed character for character into
LaTeX that compiles with pdflatex (specials escaped, Unicode mathematics mapped, unknown characters
written as [U+XXXX]); it is never summarised, reordered or edited.
"""
import re
import unicodedata

from .proof_coverage import (POST_HEADER, certified_reasoning, coverage, _record_index)

# Unicode mathematics seen in batch-3 certified texts -> LaTeX maths (wrapped in \ensuremath).
_MATH = {
    '≤': r'\le', '≥': r'\ge', '≠': r'\ne', '≈': r'\approx', '≍': r'\asymp', '∞': r'\infty', '→': r'\to',
    '←': r'\leftarrow', '↔': r'\leftrightarrow', '⇒': r'\Rightarrow', '⇐': r'\Leftarrow', '⇔': r'\Leftrightarrow',
    '⟹': r'\Longrightarrow', '⟸': r'\Longleftarrow', '⟺': r'\Longleftrightarrow', '⟶': r'\longrightarrow',
    '⟷': r'\longleftrightarrow', '↦': r'\mapsto', '↪': r'\hookrightarrow', '↠': r'\twoheadrightarrow',
    '↞': r'\twoheadleftarrow', '↓': r'\downarrow', '⤏': r'\dashrightarrow', '⇏': r'\not\Rightarrow',
    '∈': r'\in', '∉': r'\notin', '∋': r'\ni', '∌': r'\not\ni', '⊂': r'\subset', '⊆': r'\subseteq', '⊊': r'\subsetneq',
    '⊄': r'\not\subset', '⊃': r'\supset', '⊇': r'\supseteq', '⊋': r'\supsetneq', '⊐': r'\sqsupset', '⊲': r'\lhd',
    '∪': r'\cup', '∩': r'\cap', '⋃': r'\bigcup', '∖': r'\setminus', '∅': r'\emptyset', '×': r'\times',
    '·': r'\cdot', '⋅': r'\cdot', '∘': r'\circ', '±': r'\pm', '∓': r'\mp', '−': '-', '⊕': r'\oplus', '⊗': r'\otimes',
    '⊙': r'\odot', '⊛': r'\circledast', '⊞': r'\boxplus', '⊔': r'\sqcup', '⊥': r'\perp', '⊣': r'\dashv',
    '∥': r'\parallel', '‖': r'\|', '≅': r'\cong', '≇': r'\ncong', '≃': r'\simeq', '∼': r'\sim', '≡': r'\equiv',
    '≢': r'\not\equiv', '≺': r'\prec', '≻': r'\succ', '≲': r'\lesssim', '≳': r'\gtrsim', '≰': r'\not\le',
    '≪': r'\ll', '≫': r'\gg', '∝': r'\propto', '∀': r'\forall', '∃': r'\exists', '¬': r'\neg', '∧': r'\wedge',
    '∨': r'\vee', '∂': r'\partial', '∇': r'\nabla', '∫': r'\int', '∏': r'\prod', '∑': r'\sum', '√': r'\surd',
    '⟨': r'\langle', '⟩': r'\rangle', '⌊': r'\lfloor', '⌋': r'\rfloor', '⌈': r'\lceil', '⌉': r'\rceil',
    '⋯': r'\cdots', '⋆': r'\star', '★': r'\star', '∎': r'\blacksquare', '□': r'\square', '♯': r'\sharp',
    '⋈': r'\bowtie', '⌣': r'\smile', '†': r'\dagger', 'ℓ': r'\ell', '°': r'^\circ', '′': r"'",
    'ℤ': r'\mathbb{Z}', 'ℕ': r'\mathbb{N}', 'ℝ': r'\mathbb{R}', 'ℂ': r'\mathbb{C}', 'ℚ': r'\mathbb{Q}',
    'ℙ': r'\mathbb{P}', 'ℍ': r'\mathbb{H}', '𝕂': r'\mathbb{K}', '𝕊': r'\mathbb{S}', '𝔽': r'\mathbb{F}',
    '𝕌': r'\mathbb{U}', '𝕍': r'\mathbb{V}', '𝕎': r'\mathbb{W}', '𝔻': r'\mathbb{D}',
    'ℛ': r'\mathcal{R}', 'ℒ': r'\mathcal{L}', 'ℳ': r'\mathcal{M}', 'ℬ': r'\mathcal{B}', 'ℱ': r'\mathcal{F}',
    'ℋ': r'\mathcal{H}', '𝒮': r'\mathcal{S}', '𝒱': r'\mathcal{V}', '𝒟': r'\mathcal{D}', '𝒳': r'\mathcal{X}',
    '𝓛': r'\mathcal{L}', '𝔖': r'\mathfrak{S}', '𝔅': r'\mathfrak{B}',
    **{c: '\\' + n for c, n in zip('αβγδεζηθικλμνξπρστυφχψω',
        ['alpha', 'beta', 'gamma', 'delta', 'varepsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa', 'lambda', 'mu',
         'nu', 'xi', 'pi', 'rho', 'sigma', 'tau', 'upsilon', 'varphi', 'chi', 'psi', 'omega'])},
    **{c: '\\' + n for c, n in zip('ΓΔΘΛΞΠΣΦΨΩ', ['Gamma', 'Delta', 'Theta', 'Lambda', 'Xi', 'Pi', 'Sigma', 'Phi', 'Psi', 'Omega'])},
}
_SUP = dict(zip('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ', '0123456789+-n'))
_SUB = dict(zip('₀₁₂₃₄₅₆₇₈₉₊₋ₙᵢ', '0123456789+-ni'))
_TEXT = {'—': '---', '–': '--', '‘': '`', '’': "'", '“': '``', '”': "''", '…': r'\ldots{}', '§': r'\S{}',
         '•': r'\textbullet{}', '✓': '[yes]', '✔': '[yes]', '✗': '[no]', '¾': r'\ensuremath{3/4}'}
_ACCENT = {'\u0304': 'bar', '\u0302': 'hat', '\u0303': 'tilde', '\u0307': 'dot', '\u030c': 'check', '\u030a': 'mathring'}
_SPECIAL = {'\\': r'\textbackslash{}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}', '&': r'\&', '%': r'\%',
            '$': r'\$', '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}', '<': r'\textless{}', '>': r'\textgreater{}',
            '|': r'\textbar{}'}


def _char(ch):
    if ch in _SPECIAL:
        return _SPECIAL[ch]
    if ord(ch) < 128:
        return ch
    if ch in _TEXT:
        return _TEXT[ch]
    if ch in _MATH:
        return r'\ensuremath{%s}' % _MATH[ch]
    if ch in _SUP:
        return r'\textsuperscript{%s}' % _SUP[ch]
    if ch in _SUB:
        return r'\textsubscript{%s}' % _SUB[ch]
    base = unicodedata.normalize('NFD', ch)
    if len(base) == 2 and ord(base[0]) < 128 and base[1] in _ACCENT:
        return r'\ensuremath{\%s{\mathrm{%s}}}' % (_ACCENT[base[1]], base[0])
    if base and ord(base[0]) < 128:   # other accented Latin letters: keep the letter
        return base[0]
    return '[U+%04X]' % ord(ch)


def _line(text):
    """One line of plain text -> LaTeX; combining accents apply to the preceding character."""
    out, chars = [], list(text)
    i = 0
    while i < len(chars):
        ch = chars[i]
        if i + 1 < len(chars) and chars[i + 1] in _ACCENT and ord(ch) < 128 and ch.isalpha():
            out.append(r'\ensuremath{\%s{\mathrm{%s}}}' % (_ACCENT[chars[i + 1]], ch))
            i += 2
            continue
        if ch in _ACCENT:
            i += 1
            continue
        out.append(_char(ch))
        i += 1
    s = ''.join(out)
    s = re.sub(r"\\textasciicircum\{\}", r'\\textasciicircum{}', s)
    # Markdown emphasis and code, after escaping (the markers are plain ASCII).
    s = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", s)
    s = re.sub(r"`([^`]+)`", r"\\texttt{\1}", s)
    return s


def plain_to_tex(text):
    """Certified board text -> compiling LaTeX, every character kept. Paragraphs (blank-line separated)
    and list items keep their structure; Markdown headings become bold run-in lines."""
    out, in_list = [], False
    for raw in str(text or '').replace('\r', '').split('\n'):
        line = raw.rstrip()
        bullet = re.match(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.*)$", line)
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append(r'\begin{itemize}')
                in_list = True
            out.append(r'\item ' + _line(bullet.group(1)))
            continue
        if in_list and line.strip() and raw[:1] in ' \t':
            out.append(_line(line.strip()))
            continue
        if in_list:
            out.append(r'\end{itemize}')
            in_list = False
        if heading:
            out += ['', r'\noindent\textbf{%s}' % _line(heading.group(1)), '']
        else:
            out.append(_line(line) if line.strip() else '')
    if in_list:
        out.append(r'\end{itemize}')
    return '\n'.join(out).strip()


def certified_text(claim):
    """The certified argument text, board header lines removed."""
    return POST_HEADER.sub('', str(claim.get('text') or claim.get('statement') or '')).strip()


def certified(manifest):
    """Certified reasoning claims, premises before the claims that use them (stable by record)."""
    claims, index = _record_index(manifest)
    rows = sorted((c for c in claims if certified_reasoning(c)), key=lambda c: c.get('current_record') or 0)
    by_record = {c.get('current_record'): c for c in rows}
    done, out = set(), []

    def visit(c, depth=0):
        r = c.get('current_record')
        if r in done or depth > 50:
            return
        done.add(r)
        for p in c.get('premise_refs') or []:
            n = p if isinstance(p, int) else (int(re.sub(r'\D', '', str(p))) if re.search(r'\d', str(p)) else None)
            premise = index.get(n)
            if premise is not None and premise.get('current_record') in by_record:
                visit(by_record[premise['current_record']], depth + 1)
        out.append(c)
    for c in rows:
        visit(c)
    return out


def certified_proofs_md(manifest):
    """CERTIFIED-PROOFS.md for the writer: every certified claim with its full certified text."""
    rows = certified(manifest)
    lines = ['# Certified results and their certified proofs', '',
             'Each section below is one certified reasoning claim: its current record number, its premises and '
             'its complete certified text (statement and argument), copied from CLAIMS-MANIFEST.json. A result you '
             'state as proved must be one of these, and its proof in FINAL.tex must reproduce this certified '
             'argument in full: you may rewrite it in LaTeX, fix notation and spell out routine steps, but do '
             'not drop, shorten or replace steps. Any other result is unproved: state it only in "Unverified '
             'results used", marked unproved, without a proof.', '',
             '%d certified claims, %d words of certified text.' % (len(rows), sum(len(certified_text(c).split()) for c in rows)), '']
    for c in rows:
        prem = [str(p) for p in c.get('premise_refs') or []]
        lines += ['## Record %s: %s' % (c.get('current_record'), ' '.join(str(c.get('title') or c.get('key') or '').split())[:160]),
                  '', 'Premises: %s' % (', '.join(prem) if prem else 'none'), '', certified_text(c), '']
    return '\n'.join(lines)


PROOF_ENV = re.compile(r"\\begin\{proof\}(\[[^\]]*\])?(.*?)\\end\{proof\}", re.S)


def complete_proofs(tex, provenance, manifest, keep=None):
    """(new tex, actions): certified proofs written into every result stated as proved without one.

    Only results stated as proved are touched. A bound result whose proof is missing or not written out
    gets the certified text of the first certified record PROVENANCE.md lists for its label, after the
    writer's own proof text (which is kept). An unbound result is retitled "Not proved here" and its proof
    environment, if any, relabelled "Unverified argument"."""
    report = coverage(tex, provenance, manifest)
    claims, index = _record_index(manifest)
    actions, edits = [], []
    env_iter = list(re.finditer(r"\\begin\{(theorem|proposition|lemma|corollary|claim|fact)\*?\}(\s*\[(?:[^\[\]]|\[[^\]]*\])*\])?(.*?)\\end\{\1\*?\}", tex, re.S))
    by_label = {}
    for m in env_iter:
        lab = re.search(r"\\label\{([^}]*)\}", m.group(3) or '') or re.search(r"\\label\{([^}]*)\}", tex[m.end():m.end() + 80].split('\n', 1)[0])
        if lab:
            by_label.setdefault(lab.group(1), m)
    inserted = 0
    starts = sorted([m.start() for m in env_iter] + [h.start() for h in re.finditer(r"\\(?:sub)*section\*?\{", tex)] + [len(tex)])
    for r in report['results']:
        if not r['stated_as_proved'] or r['written_out'] or r['label'] not in by_label:
            continue
        m = by_label[r['label']]
        nxt = next((s for s in starts if s > m.end()), len(tex))
        pm = PROOF_ENV.search(tex, m.end(), nxt)
        if r['certified_records'] and keep is not None and inserted >= keep:
            # Page budget: certified, but its proof does not fit; it is not stated as proved here.
            head_end = m.start() + len('\\begin{%s}' % m.group(1)) + (1 if tex[m.start():].startswith('\\begin{%s*}' % m.group(1)) else 0)
            old = (m.group(2) or '').strip()
            edits.append((head_end, head_end + len(m.group(2) or ''), '[Certified; proof not included%s]' % ('; ' + old[1:-1] if old else '')))
            if pm:
                edits.append((pm.start(), pm.start(2), '\\begin{proof}[Outline]'))
            actions.append(dict(label=r['label'], action='certified-proof-not-included', record=r['certified_records'][0]))
        elif r['certified_records']:
            inserted += 1
            claim = index[r['certified_records'][0]]
            body = plain_to_tex(certified_text(claim))
            if pm:
                edits.append((pm.start(2), pm.end(2), pm.group(2).rstrip() + '\n\n' + body + '\n'))
            else:
                edits.append((m.end(), m.end(), '\n\\begin{proof}\n' + body + '\n\\end{proof}\n'))
            actions.append(dict(label=r['label'], action='certified-proof-inserted', record=claim.get('current_record'),
                                writer_proof_words=r['proof_words'], certified_words=r['argument_words']))
        else:
            head_end = m.start() + len('\\begin{%s}' % m.group(1)) + (1 if tex[m.start():].startswith('\\begin{%s*}' % m.group(1)) else 0)
            old = (m.group(2) or '').strip()
            title = '[Not proved here%s]' % ('; ' + old[1:-1] if old else '')
            edits.append((head_end, head_end + len(m.group(2) or ''), title))
            if pm:
                edits.append((pm.start(), pm.start(2), '\\begin{proof}[Unverified argument]'))
            actions.append(dict(label=r['label'], action='marked-not-proved', writer_proof_words=r['proof_words']))
    for a, b, text in sorted(edits, reverse=True):
        tex = tex[:a] + text + tex[b:]
    return tex, actions


def assembled_report(manifest, title='Solution to Problem N'):
    """A complete report from certified claims alone (no model draft exists)."""
    rows = certified(manifest)
    parts = [r'\documentclass[12pt]{article}', r'\usepackage{fullpage}', r'\usepackage{amsmath,amssymb,amsthm}',
             r'\newtheorem{proposition}{Proposition}', r'\begin{document}', r'\title{%s}' % _line(title), r'\author{}',
             r'\date{}', r'\maketitle', r'\section{Status}',
             'No writer produced a report. This document states the results certified during the research, each '
             'followed by its certified argument, transcribed verbatim. It does not state whether the problem is solved.',
             r'\section{Results proved here}']
    if not rows:
        parts.append('No result was certified.')
    for c in rows:
        parts += [r'\begin{proposition}\label{res:%s}' % c.get('current_record'),
                  _line(' '.join(str(c.get('statement') or c.get('title') or c.get('key') or '').split())),
                  r'\end{proposition}', r'\begin{proof}', plain_to_tex(certified_text(c)), r'\end{proof}', '']
    parts += [r'\end{document}', '']
    provenance = '\n'.join(['# Provenance', ''] + ['- `res:%s`: record %s' % (c.get('current_record'), c.get('current_record')) for c in rows]) + '\n'
    return '\n'.join(parts), provenance


# ------------------------------------------------------------------ compile handling and page budget

# Undefined commands with a known home package (operator rule: no layout packages are added).
PACKAGE_OF = {'mathscr': 'mathrsfs', 'mathbb': 'amssymb', 'mathfrak': 'amssymb', 'cref': 'cleveref', 'Cref': 'cleveref',
              'operatorname': 'amsmath', 'DeclareMathOperator': 'amsmath', 'eqref': 'amsmath', 'text': 'amsmath',
              'lesssim': 'amssymb', 'gtrsim': 'amssymb', 'blacksquare': 'amssymb', 'twoheadrightarrow': 'amssymb',
              'dashrightarrow': 'amssymb', 'circledast': 'amssymb', 'boxplus': 'amssymb', 'twoheadleftarrow': 'amssymb',
              'lhd': 'amssymb', 'square': 'amssymb', 'nsubseteq': 'amssymb', 'ncong': 'amssymb', 'mathcal': None}
UNDEFINED = re.compile(r"^! Undefined control sequence\.\s*\n((?:.*\n?){1,3})", re.M)


def repair_undefined(tex, log):
    """(tex, fix) for the first undefined control sequence in a pdflatex log, or (tex, None).

    Only the preamble changes: a missing package for a known command, an alias for a case variant of a
    defined macro (\\kl for \\KL), or \\providecommand as an upright operator name. The body is untouched."""
    m = UNDEFINED.search(log)
    if not m:
        return tex, None
    # TeX prints the context with the offending token last on the first line: "\\Sbar ->\\mathscr" when it
    # came from a macro expansion, "l.572 =:\\kl" when it is in the source line itself.
    name = next((t.group(1) for line in m.group(1).splitlines() for t in [re.search(r"\\([A-Za-z]+)\s*$", line)] if t), None)
    if not name:
        return tex, None
    at = tex.find('\\begin{document}')
    if at < 0:
        return tex, None
    defined = set(re.findall(r"\\(?:re)?newcommand\*?\{?\\([A-Za-z]+)\}?|\\DeclareMathOperator\*?\{\\([A-Za-z]+)\}|\\def\\([A-Za-z]+)", tex[:at]))
    defined = {x for t in defined for x in t if x}
    variant = next((d for d in sorted(defined) if d.lower() == name.lower() and d != name), None)
    if PACKAGE_OF.get(name):
        line = r'\usepackage{%s}' % PACKAGE_OF[name]
        fix = 'package %s for \\%s' % (PACKAGE_OF[name], name)
    elif variant:
        line = r'\providecommand{\%s}{\%s}' % (name, variant)
        fix = 'alias \\%s for \\%s' % (name, variant)
    else:
        line = r'\providecommand{\%s}{\operatorname{%s}}' % (name, name)
        fix = 'upright operator \\%s' % name
    if line in tex[:at]:
        return tex, None
    return tex[:at] + '% harness compile repair: ' + fix + '\n' + line + '\n' + tex[at:], fix


def _with_packages(tex, needed=('amssymb',)):
    """Packages the transcribed certified text needs (amssymb symbols), added only when absent."""
    at = tex.find('\\begin{document}')
    pre = tex[:at] if at >= 0 else ''
    add = [p for p in needed if not re.search(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b%s\b" % p, pre)]
    if at < 0 or not add:
        return tex
    return tex[:at] + ''.join(r'\usepackage{%s}' % p + '\n' for p in add) + tex[at:]


def finalize(tex, provenance, manifest, compile_fn, *, page_limit=16, max_repairs=6):
    """Certified proofs completed into a writer draft, compiled within the page limit.

    compile_fn(tex) -> dict(ok, pages, log). Returns dict(tex, compile, actions, repairs, omitted).
    When every completed proof does not fit in page_limit pages, the latest results in document order
    keep their writer text and are retitled "Certified; proof not included" so that no result is stated
    as proved without its proof; the writer's own proofs are never cut."""
    full, actions = complete_proofs(tex, provenance, manifest)
    repairs = []

    def build(source):
        source = _with_packages(source)
        result = compile_fn(source)
        for _ in range(max_repairs):
            if result['ok']:
                break
            source, fix = repair_undefined(source, result.get('log', ''))
            if not fix:
                break
            if fix not in repairs:
                repairs.append(fix)
            result = compile_fn(source)
        return source, result

    source, result = build(full)
    inserted = [a for a in actions if a['action'] == 'certified-proof-inserted']
    omitted = []
    if result['ok'] and (result.get('pages') or 0) > page_limit and inserted:
        lo, hi, best = 0, len(inserted), None
        while lo < hi:  # largest k: the first k insertions (document order) fit the page limit
            k = (lo + hi + 1) // 2
            trial, _ = complete_proofs(tex, provenance, manifest, keep=k)
            s, r = build(trial)
            if r['ok'] and (r.get('pages') or 0) <= page_limit:
                lo, best = k, (s, r)
            else:
                hi = k - 1
        trial, _ = complete_proofs(tex, provenance, manifest, keep=lo)
        source, result = best if best else build(trial)
        omitted = [a['label'] for a in inserted[lo:]]
    return dict(tex=source, compile=result, actions=actions, repairs=repairs, omitted=omitted)
