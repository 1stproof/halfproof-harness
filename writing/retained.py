"""Stage a failed attempt's explicit roster choice for measurement/continuation.

No mathematical selection or editing occurs here. Ambiguous editor choices fail.
The staged file is an input artifact, never a published FINAL delivery.
"""
import json
from pathlib import Path
import shutil
from .core import digest,parse,save
from .runtime import clean_tex


def collect_retained_candidates(origin):
    """Inventory all authored drafts; the roster, never this function, chooses."""
    origin=Path(origin);authored={}
    for response in sorted(origin.glob('calls/*/response.json')):
        try:
            raw=json.loads(response.read_text())['choices'][0]['message']['content']
            authored.setdefault(digest(clean_tex(raw).encode()),[]).append(str(response))
        except (ValueError,KeyError,TypeError,IndexError):continue
    candidates=[]
    for text in sorted(origin.glob('candidate-*/FINAL.tex')):
        sha=digest(text.read_bytes())
        if sha not in authored:raise ValueError('retained draft lacks exact roster response')
        directory=text.parent;review=directory/'referee.json';raw_review=directory/'referee.txt'
        candidates.append(dict(candidate=len(candidates),directory=str(directory),
            manuscript=text.read_text(),sha256=sha,author_responses=authored[sha],
            compilation=json.loads((directory/'compilation.json').read_text()),
            review=json.loads(review.read_text()) if review.exists() else
                dict(unparsed_roster_review=raw_review.read_text() if raw_review.exists() else 'No roster review saved.')))
    if not candidates:raise ValueError('no authored retained drafts')
    return candidates


def stage_selected_candidate(origin,seed):
    origin=Path(origin);seed=Path(seed)
    choices=[]
    for response in sorted(origin.glob('calls/selection-*/response.json')):
        try:
            data=json.loads(response.read_text())
            selection=parse(data['choices'][0]['message']['content'])
            if type(selection.get('candidate')) is int:
                choices.append((selection['candidate'],str(response)))
        except (ValueError,KeyError,TypeError,IndexError):continue
    ids={n for n,_ in choices}
    if len(ids)!=1:raise ValueError('no unambiguous saved roster-editor choice')
    chosen=next(iter(ids))
    candidates=json.loads((origin/'candidate-index.json').read_text())
    matches=[c for c in candidates if c['candidate']==chosen]
    if len(matches)!=1:raise ValueError('editor choice absent from candidate index')
    candidate=matches[0];text=Path(candidate['directory'])/'FINAL.tex'
    if digest(text.read_bytes())!=candidate['sha256']:raise ValueError('chosen candidate changed')
    authors=[]
    for response in sorted(origin.glob('calls/*/response.json')):
        try:
            raw=json.loads(response.read_text())['choices'][0]['message']['content']
            if digest(clean_tex(raw).encode())==candidate['sha256']:authors.append(str(response))
        except (ValueError,KeyError,TypeError,IndexError):continue
    if not authors:raise ValueError('chosen candidate has no exact roster response')
    marker=dict(candidate_only=True,origin=str(origin),candidate=chosen,
        selector_responses=[p for _,p in choices],author_responses=authors,sha256=candidate['sha256'],
        candidate_compilation={k:candidate['compilation'].get(k) for k in ('ok','pages')},
        purpose='retained roster-selected candidate for measurement and own-roster continuation; not a delivery')
    if seed.exists():
        if json.loads((seed/'CANDIDATE-INPUT.json').read_text())!=marker:
            raise ValueError('staged candidate identity changed')
        if digest((seed/'FINAL.tex').read_bytes())!=marker['sha256']:
            raise ValueError('staged candidate text changed')
        return seed
    seed.mkdir(parents=True)
    for name in ('CLAIMS-MANIFEST.json','ORIGINAL-PROBLEM.tex','plan.json'):
        shutil.copyfile(origin/name,seed/name)
    shutil.copyfile(text,seed/'FINAL.tex')
    save(seed/'CANDIDATE-INPUT.json',marker)
    return seed
