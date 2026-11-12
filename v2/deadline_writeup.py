"""Publish the newest retained model draft without changing any check verdict."""
import json
from pathlib import Path
import time
from .closure_build import FALLBACK
from .closure_io import canonical, read_file, sha256, publish_tree, verify_tree

GATE_TEXT=b'% This report was produced by the harness final-report check:'


def model_text(data):
    """One placeholder test for every capture path: empty, FALLBACK or report-check text is not a draft."""
    return bool(data and data.strip()) and data!=FALLBACK and not data.startswith(GATE_TEXT)


def capture(catalog, sources):
    """Called by the isolated publisher after owned writers have stopped."""
    from .closure_build import BuildResult, report_dependencies
    for source in sources:
        root=Path(source['root'])
        for name in ('FINAL-DRAFT.tex','FINAL-REJECTED.tex','FINAL.tex','FINAL.md'):
            try:
                data=read_file(root,name)
                if not model_text(data):continue
                stamp=(root/name).stat().st_mtime_ns
                files={'FINAL.md' if name.endswith('.md') else 'FINAL.tex':data}
                if name=='FINAL.tex':
                    try:
                        files=report_dependencies(root)
                    except (OSError,ValueError):pass
                result=BuildResult(False,files,json.dumps(source.get('checks',{}),sort_keys=True),
                    dict(outcome='not-run',writeup_time_ns=stamp,check_status=source.get('checks',{})))
                # Work copies: retained identical bytes keep their author save time.
                catalog.retain_failed_source(result,author_id=source['author_id'],model_id=source['model_id'],copy=True)
            except (OSError,ValueError):continue


def candidates(artifacts, state):
    """Legacy receipt adapter; selection and catalog parsing use deadline_delivery."""
    from .deadline_delivery import catalog_candidates
    run=state['problem_id'].removeprefix('b3-')
    result=[]
    for candidate in catalog_candidates(artifacts,run,scope=(state['problem_id'],state['arm_id'])):
        row=candidate['record']
        # Common parser already excludes unbound PDFs and damaged artifacts.
        adapted=dict(row,files={n:sha256(b) for n,b in candidate['files'].items()},
                     internal_status=candidate['verification'])
        result.append(dict(row=adapted,directory=candidate['origin'],
            name='FINAL.tex' if 'FINAL.tex' in candidate['files'] else 'FINAL.md',
            writeup_time_ns=candidate['captured_at_ns'],candidate=candidate))
    return result


def publish(destination, choices, *, run, checks=None):
    """No provider call or compile. Existing PDF belongs to this exact bundle only."""
    if not choices:return None
    from .deadline_delivery import latest_key
    chosen=max(choices,key=lambda x:latest_key(x['candidate']))
    row=chosen['row'];directory=Path(chosen['directory'])
    files={n:read_file(directory,n) for n in row['files'] if n not in ('bundle.json','DELIVERY.md')}
    compiled=row.get('compilation',{}).get('outcome')=='pass' and files.get('FINAL.pdf',b'').startswith(b'%PDF-')
    if not compiled:files.pop('FINAL.pdf',None)
    exact_checks=dict(candidate_sha256=row.get('candidate_sha256'),
                      internal_status=row.get('internal_status','completion-unconfirmed'),
                      status_evidence=row.get('status_evidence',[]),compilation=row.get('compilation',{}),
                      reviewed_checks=checks,
                      notice='Reviewed checks may concern an earlier candidate; no verdict is transferred to this draft.')
    files['CHECKS.json']=canonical(exact_checks)
    files['DELIVERY.md']=(
        '# Latest model writeup — deadline delivery\n\n'
        'This is the newest retained model draft, released even if checks failed or did not finish. '
        'Any claim not certified is **unverified**. This delivery confers no new certification.\n\n'
        f"Compilation: {'compiled PDF available for this exact bundle' if compiled else 'not successfully compiled; source delivered'}.\n\n"
        f"Exact-draft check status: {exact_checks['internal_status']}. See CHECKS.json for failed, missing, or completed checks and existing evidence. "
        'An absent check is not a pass. No mathematical text was repaired.\n').encode()
    manifest=dict(schema='deadline-writeup-v1',run=run,authorship='model',correctness='unverified',
                  source_bundle=row['bundle_id'],writeup_time_ns=chosen['writeup_time_ns'],
                  compilation='pass' if compiled else 'source-only',checks=exact_checks,
                  files={n:sha256(b) for n,b in sorted(files.items())})
    ident=sha256(canonical(manifest))
    publish_tree(Path(destination),ident,{**files,'delivery.json':canonical(manifest)})
    return dict(bundle_id=ident,**manifest)


def verify(destination, record):
    metadata={k:v for k,v in record.items() if k!='bundle_id'}
    if (record.get('schema')!='deadline-writeup-v1' or record.get('authorship')!='model'
            or record.get('correctness')!='unverified' or sha256(canonical(metadata))!=record['bundle_id']):
        raise ValueError('invalid latest-writeup provenance')
    root=Path(destination)/record['bundle_id']
    verify_tree(root,{**record['files'],'delivery.json':sha256(canonical(metadata))})
    if not any(n in record['files'] for n in ('FINAL.tex','FINAL.md')) or 'DELIVERY.md' not in record['files']:
        raise ValueError('latest writeup requires source and visible check notice')
    return root
