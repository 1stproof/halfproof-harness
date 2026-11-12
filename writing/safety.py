"""Mechanical provenance checks and deterministic, roster-only delivery.

This checks declared bindings, never whether a statement follows mathematically.
"""
import json
import os
import subprocess
import math
import re
from pathlib import Path
from .core import atomic_write, digest, save

DEFAULT_POLICY = dict(attempts=1, revisions=2, repair_rounds=2, transport_retries=1,
    request_seconds=3600, deadline=None,
    selection_rule='compiled-accepted-editor-then-first-best')


from contracts.route import pinned_route


PUBLICATION_MARGIN = 120


def allocation_deadline():
    end = os.environ.get('SLURM_JOB_END_TIME')
    if end is None and os.environ.get('SLURM_JOB_ID'):
        raw = subprocess.check_output(['scontrol','show','job',os.environ['SLURM_JOB_ID'],'-o'], text=True)
        match = re.search(r'EndTime=(\S+)', raw)
        if match and match[1] not in ('Unknown','N/A'):
            from datetime import datetime
            end = datetime.fromisoformat(match[1]).timestamp()
    if end is None:
        raise ValueError('writer requires an absolute deadline or SLURM allocation end time')
    return float(end) - PUBLICATION_MARGIN


def policy(run):
    result = dict(DEFAULT_POLICY, **run.get('writer_policy', {}))
    for k in ('attempts','revisions','repair_rounds','transport_retries','request_seconds'):
        if type(result[k]) is not int or result[k] < (1 if k in ('attempts','request_seconds') else 0):
            raise ValueError('invalid writer policy '+k)
    if result['selection_rule'] != DEFAULT_POLICY['selection_rule']:
        raise ValueError('unknown precommitted selection rule')
    if result['deadline'] is None:
        result['deadline'] = allocation_deadline()
    if not isinstance(result['deadline'], (int,float)) or not math.isfinite(result['deadline']):
        raise ValueError('deadline must be absolute Unix seconds')
    return result


def statement_diagnostics(source, closure):
    # Preserve newlines and provenance comments; ignore ordinary comments and verbatim.
    # Environment aliases declared with newtheorem are included, even arbitrary names.
    lines = source.splitlines()
    kinds = {'theorem','lemma','proposition','corollary'}
    declarations = re.findall(r'\\newtheorem\*?\s*\{([^}]+)\}(?:\s*\[[^]]+\])?\s*\{([^}]+)\}', source)
    names = kinds | {alias for alias, title in declarations if title.strip().lower() in kinds}
    code = '\n'.join(re.split(r'(?<!\\)%', line)[0] for line in lines)
    diagnostics = []
    labels = re.findall(r'\\label\s*\{([^}]+)\}', code)
    starts = list(re.finditer(r'\\begin\s*\{([^}]+)\}', code))
    for match in starts:
        env = match[1]
        if env.rstrip('*') not in names: continue
        line = code[:match.start()].count('\n') + 1
        end = re.search(r'\\end\s*\{'+re.escape(env)+r'\}', code[match.end():])
        body = code[match.end():match.end()+end.start()] if end else ''
        own_labels = re.findall(r'\\label\s*\{([^}]+)\}', body)
        # A declaration belongs immediately above the environment or inside it.
        original = '\n'.join(lines[line-1:line + body.count('\n')])
        above = line-2
        while above >= 0 and not lines[above].strip(): above -= 1
        if above >= 0 and lines[above].lstrip().startswith('%'): original = lines[above]+'\n'+original
        records = re.findall(r'%\s*RECORDS:\s*([0-9, ]+)', original)
        ids = [int(n) for row in records for n in re.findall(r'\d+',row)]
        exempt = bool(re.search(r'%\s*STATUS:\s*(?:cited|conjectural)\b', original, re.I))
        reasons = []
        if not end: reasons.append('unterminated statement')
        if len(own_labels)!=1: reasons.append('statement requires exactly one label')
        elif labels.count(own_labels[0])!=1: reasons.append('duplicate statement label')
        if not exempt and (not ids or not set(ids)<=set(closure)):
            reasons.append('RECORDS must map label to records inside certified closure, or STATUS: cited/conjectural')
        if reasons:
            diagnostics.append(dict(line=line,environment=env,label=own_labels[0] if own_labels else None,
                records=ids,message='paper.tex:'+str(line)+': error: '+'; '.join(reasons)))
    return diagnostics


def retain(out, manuscript, directory, compilation, closure, candidates):
    """Publish durable source and index before any next request."""
    directory = Path(directory);directory.mkdir(parents=True,exist_ok=True)
    atomic_write(directory/'FINAL.tex', manuscript.encode())
    c = dict(candidate=len(candidates),directory=str(directory),manuscript=manuscript,
        compilation=compilation,review={},sha256=digest(manuscript.encode()),
        violations=statement_diagnostics(manuscript,closure))
    candidates.append(c)
    checkpoint(out,candidates)
    return c


def checkpoint(out,candidates):
    save(Path(out)/'candidate-index.json',[{k:v for k,v in c.items() if k!='manuscript'} for c in candidates])
    root = Path(out).parent
    if (root/'precommit.json').exists():
        from .control import publish_checkpoint
        publish_checkpoint(root)


def choose(candidates, editor=None):
    if not candidates: return None
    def rank(c):
        return (bool(c['compilation'].get('ok')),not bool(c.get('violations')),
                bool(c.get('review',{}).get('accept')))
    best = max(map(rank,candidates))
    eligible = [c for c in candidates if rank(c)==best]
    if type(editor) is int:
        for c in eligible:
            if c['candidate']==editor:return c
    return eligible[0]  # stable, never latest-save or model-role priority


def deliver(out, candidates, plan, editor=None, flags=(), contract_hash=None):
    """Single idempotent publisher; manifest is the commit marker written last."""
    out=Path(out);target=out/'delivery';target.mkdir(parents=True,exist_ok=True)
    c=choose(candidates,editor)
    flags=list(flags)
    if c is None: flags.append('no_complete_candidate')
    else:
        if not c['compilation'].get('ok'):flags.append('source_only')
        if not c.get('review',{}).get('accept'):flags.append('unaccepted_roster_review')
        if c.get('violations'):flags.append('closure_violations')
        if editor!=c['candidate']:flags.append('deterministic_fallback')
    source=c['manuscript'].encode() if c else b''
    pdfpath=Path(c['directory'])/'FINAL.pdf' if c else None
    pdf=pdfpath.read_bytes() if c and c['compilation'].get('ok') and pdfpath.exists() else b''
    if not pdf:flags.append('pdf_unavailable')
    receipts=[]
    for path in sorted(list((out/'calls').glob('*/receipt.json')) + list(out.glob('attempt-*/calls/*/receipt.json'))):
        r=json.loads(path.read_text());receipts.append(dict(request=str(path.parent.relative_to(out)),**r))
    costs=[r['cost_usd'] for r in receipts if isinstance(r.get('cost_usd'),(int,float))]
    manifest=dict(schema=1,selected_candidate=c['candidate'] if c else None,
        record_ids=plan.get('main_records',[]),closure=plan.get('included_records',[]),
        violations=c.get('violations',[]) if c else [],flags=sorted(set(flags)),
        contract_sha256=contract_hash,requests=receipts,
        costs=dict(usd=sum(costs),priced=len(costs),sent=len(receipts),unknown=len(receipts)-len(costs)),
        hashes=dict(paper_tex=digest(source),paper_pdf=digest(pdf)),
        compilation=c['compilation'] if c else {},
        selection_rule=DEFAULT_POLICY['selection_rule'])
    atomic_write(target/'paper.tex',source);atomic_write(target/'paper.pdf',pdf)
    log=json.dumps(dict(compilation=manifest['compilation'],flags=manifest['flags']),indent=2).encode()
    atomic_write(target/'delivery.log',log)
    save(target/'manifest.json',manifest)
    # Compatibility artifacts are exact copies of the same publication.
    atomic_write(out/'FINAL.tex',source)
    if pdf:atomic_write(out/'FINAL.pdf',pdf)
    return manifest
