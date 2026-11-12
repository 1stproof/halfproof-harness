"""Exposition instructions, independent of mathematical scope and acceptance."""
PAPER = (
    ' Use exactly \\documentclass[12pt]{article} and \\usepackage{fullpage} as required by SOLUTION-TEMPLATE.tex. Write a mathematics paper in amsart style: title, concise abstract, Status, definitions and notation, '
    'precise theorem/lemma/proposition statements, complete proofs, what remains open, and bibliography. '
    'Use amsmath and amsthm theorem environments and consistent labels. Define notation before use. '
    'Use only a small set of well-chosen macros for repeated mathematical objects; never define a macro '
    'for one-off notation or ordinary prose. Proofs use full sentences with grammatical displayed equations. '
    'Citations use cite commands and a bibliography, never raw URLs as substitutes for references. '
    'Keep source results separate from results proved here and distinguish every conditional dependency. '
    'FINAL.tex and its PDF contain mathematics only. Put record mappings and acceptance justification '
    'in PROVENANCE.md; each mapping names the exact theorem label, session and proposal record number.')
EDITORIAL = (
    ' Use exactly \\documentclass[12pt]{article} and \\usepackage{fullpage} as required by SOLUTION-TEMPLATE.tex. Prepare a publishable mathematical exposition using amsart structure. Before saving, perform an '
    'editorial pass: introduce each definition before its first use; make each theorem self-contained with '
    'all hypotheses and quantifiers; organize notation consistently; write proofs as connected full sentences '
    'with explanations between displayed equations. Use amsthm theorem and proof environments. Keep macros '
    'few, mnemonic and reserved for genuinely repeated notation; expand all one-off macros. Keep the abstract '
    'concise, the Status precise, and separate proved results, attributed literature and open or conditional '
    'statements. Place citations in a bibliography. Do not alter mathematical content or add any argument. '
    'Remove procedural narration from FINAL.tex. Save all board mapping and justification in PROVENANCE.md, '
    'with exact LaTeX labels, session names and proposal record numbers. Re-read for mathematical grammar.')
VARIANTS = {'paper': PAPER, 'editorial': EDITORIAL}

# A third, generic scope-control variant; it carries no problem-specific feedback.
STRICT = PAPER + (
    ' Eligibility is whole-claim eligibility: an accepted subargument or a favourable review of part '
    'of an unaccepted parent does not license an unconditional statement from that parent. Only the '
    'exact rows marked accepted in CLAIMS-LEDGER.md license results proved here. Do not broaden their '
    'scope, add corollaries, or manufacture missing arguments. Source-checked literature means an '
    'explicit literature-type row marked source-checked (or accepted); a legacy reasoning-type row '
    'does not become a literature row because its text mentions a paper. If there are no eligible '
    'literature rows, leave that section brief and make no quoted theorem claim. Bibliographical '
    'background may still be attributed as background without asserting a proof or an application. '
    'Choose a small number of eligible advances and reproduce their complete accepted arguments. '
    'Omit unverified material unless essential to explaining what remains open; then give only the '
    'conditional statement, never a proof. The Status describes the mathematical boundary of the '
    'answer, without narrating review or record history. Keep all acceptance decisions, record '
    'identifiers, search logs, and audit explanations exclusively in PROVENANCE.md. '
    'Finish saving both files before continuing optional evidence reads.')
VARIANTS['strict'] = STRICT

# Frozen-seed comparison 2026-10-11 (40 arms, blinded Codex grading, final-report check as gate):
# Qwen 3.8 2.4T with the editorial prompt was the only pair faithful and gate-passing on both seeds.
# Flash stays as backup: a different provider, fast and cheap; it writes only when the primary has
# no compiled report. K3 (0 of 8 faithful, slowest and most expensive) is no longer a writer.
PRIMARY = 'qwen24t'
BACKUP = 'dsf'
DEFAULT_VARIANT = 'editorial'
DEFAULT_STYLE = VARIANTS[DEFAULT_VARIANT]

def writer_pair(strategy, final_author='dsf'):
    """Writer identities are independent of the research orchestrator."""
    return (PRIMARY, BACKUP) if strategy == 'mixed-swarm' else (final_author,'dsf')
