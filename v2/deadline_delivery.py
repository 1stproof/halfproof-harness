"""Release the newest saved writeup without granting scientific certification.

No model calls, compilation, scheduler mutation, accounting change or runtime
freeze happens here. Immutable byte snapshots can outlive a crashed closing.
"""
import json
import time
from pathlib import Path

from .closure_io import (canonical, read_file, sha256, publish_tree, verify_tree,
                         atomic_json, ClosureError)


def notice(*, reason, compilation, verification, diagnostics='', checks=None):
    return ('# Deadline writeup release\n\n'
            'Any claim not certified is unverified.\n\n'
            f'Release reason: {reason}.\n'
            f'Compilation: {compilation}.\n'
            f'Exact-source verification: {verification}.\n'
            'Failed or incomplete checks: '+json.dumps(checks,sort_keys=True)+'.\n'
            'A null check record means the check was not reached or its result is unavailable.\n\n'
            'Compiler diagnostics:\n\n'+(diagnostics or 'Not available.')+'\n').encode()


def release_files(run, files, destination, *, reason, captured_at_ns,
                  compilation='not-reached', verification='completion-unconfirmed',
                  diagnostics='', checks=None, source_origin=None):
    """Publish a frozen copy of bytes, not a scientific/accounting fence."""
    source = files.get('FINAL.tex') or files.get('FINAL.md')
    placeholder = not source
    if placeholder:
        files={'FINAL.md':b'# No writeup draft exists\n\nNo model draft was saved before delivery.\n'}
    else:
        files={k:v for k,v in files.items() if k not in ('bundle.json','DELIVERY.md')}
    files['DELIVERY.md']=notice(reason=reason,compilation=compilation,
        verification=verification,diagnostics=diagnostics,checks=checks)
    metadata=dict(schema=1,run=run,captured_at_ns=captured_at_ns,
                  reason=reason,source_origin=source_origin,compilation=compilation,
                  verification=verification,diagnostics=diagnostics,checks=checks,
                  files={k:sha256(v) for k,v in sorted(files.items())},
                  correctness='unverified',placeholder=placeholder,
                  scientific_freeze=False)
    ident=sha256(canonical(metadata))
    destination=Path(destination)
    publish_tree(destination,ident,{**files,'release.json':canonical(metadata)})
    return dict(schema='autoctl-delivery-v1',run=run,frozen=True,
                scientific_freeze=False,model_delivery='failed' if placeholder else 'degraded',
                placeholder=placeholder,correctness='unverified',captured_at_ns=captured_at_ns,
                files={str((destination/ident/k).absolute()):v for k,v in metadata['files'].items()},
                release_sha256=ident,source_origin=source_origin,compilation=compilation,
                verification=verification,diagnostics=diagnostics,checks=checks)


def latest_key(candidate):
    """One ordering for native close, timer, controller and legacy sidecars."""
    return (candidate['captured_at_ns'], 'FINAL.pdf' in candidate['files'],
            candidate.get('origin') or '')


def record_time(record, state, fallback=0):
    matching=[d for d in state.get('drafts',[]) if d['candidate_sha256']==record['candidate_sha256']]
    return max((d['captured_at_ns'] for d in matching),default=(
        record.get('captured_at_ns') or record.get('writeup_time_ns') or
        record.get('compilation',{}).get('writeup_time_ns') or fallback))


def catalog_candidates(root, run, *, scope=None):
    """Read only the trusted catalog and its hash-bound, bounded artifact trees."""
    root=Path(root)
    scope=scope or ('b3-'+run,'mixed-swarm')
    def in_scope(problem,arm):return arm==scope[1] and problem in (scope[0],run if scope[0]=='b3-'+run else scope[0])
    index_failures=[]
    try:
        state=json.loads(read_file(root,'catalog-state.json',64*1024*1024))
        if not in_scope(state['problem_id'],state['arm_id']):
            raise ClosureError('foreign delivery catalog')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        if isinstance(exc,ClosureError) and str(exc)=='foreign delivery catalog':raise
        index_failures=[type(exc).__name__+': '+str(exc)[:300]]
        # A damaged index is a failed check, not authority to discard immutable
        # drafts. Inspect only bounded, scope-bound snapshot manifests.
        state=dict(problem_id='b3-'+run,arm_id='mixed-swarm',terminal=None)
        for kind,key in (('drafts','drafts'),('bundles','bundles'),('degraded','failed_builds')):
            directory=root/kind;state[key]=[]
            if not directory.is_dir() or directory.is_symlink():continue
            snapshots=list(directory.iterdir())
            if len(snapshots)>10000:raise ClosureError('draft snapshot inventory bound exceeded')
            for snapshot in snapshots:
                if len(snapshot.name)!=64 or any(c not in '0123456789abcdef' for c in snapshot.name):continue
                try:
                    metadata=json.loads(read_file(snapshot,'bundle.json'))
                    if (not in_scope(metadata.get('problem_id'),metadata.get('arm_id'))
                            or sha256(canonical(metadata))!=snapshot.name):continue
                    row=dict(bundle_id=snapshot.name,**metadata)
                    row.setdefault('internal_status','completion-unconfirmed')
                    state[key].append(row)
                except (OSError,ValueError,KeyError,TypeError):continue
    rows=[]
    for kind,key in (('drafts','drafts'),('bundles','bundles'),('degraded','failed_builds')):
        for record in state.get(key,[]):
            if record.get('authorship')!='model' or record.get('compilation',{}).get('report_gate_fallback'):
                continue
            base=root/kind/record['bundle_id']
            integrity=[]
            try:
                metadata=json.loads(read_file(base,'bundle.json'))
                if sha256(canonical(metadata))!=record['bundle_id'] or metadata['files']!=record['files']:
                    integrity.append('draft bundle identity differs')
                verify_tree(base,{**record['files'],'bundle.json':record['bundle_id']})
            except (OSError,ValueError,KeyError,TypeError) as exc:
                integrity.append(type(exc).__name__+': '+str(exc)[:300])
            files={}
            for name in record['files']:
                try:files[name]=read_file(base,name)
                except (OSError,ValueError) as exc:integrity.append(name+': '+str(exc)[:300])
            if integrity:
                # A failed check changes the release label, never substitutes a
                # placeholder for readable source. An unbound PDF is excluded.
                files.pop('FINAL.pdf',None)
            if not (files.get('FINAL.tex') or files.get('FINAL.md')):continue
            captured=record_time(record,state,max((base/n).stat().st_mtime_ns for n in files))
            # Build completion must not make an older candidate newer than its snapshot.
            matching=[d for d in state.get('drafts',[]) if d['candidate_sha256']==record['candidate_sha256']]
            if matching:captured=max(d['captured_at_ns'] for d in matching)
            rows.append(dict(files=files,captured_at_ns=captured,origin=str(base),record=record,
                candidate=record['candidate_sha256'],compilation='not-reached-for-current-bytes' if integrity else record.get('compilation',{}).get('outcome','not-reached'),
                verification=('integrity-failed; unverified' if integrity else 'internal-rejected' if record['candidate_sha256'] in state.get('rejected_candidates',{}) else record.get('internal_status','completion-unconfirmed')),
                diagnostics=record.get('diagnostics',''),checks=dict(catalog_index_failures=index_failures,integrity_failures=integrity,semantic=(state.get('terminal') or {}).get('reviewed_check_summary'),mechanical=record.get('checks') or next((d.get('checks') for d in reversed(matching) if d.get('checks')),None))))
    return rows


def publish_latest(run, roots, destination, *, reason='deliver-by', prior_receipt=None):
    candidates=[];legacy_prior=[]
    if prior_receipt and Path(prior_receipt).exists():
        prior=json.loads(read_file(Path(prior_receipt).parent,Path(prior_receipt).name))
        if prior.get('run')!=run:raise ClosureError('foreign prior delivery')
        if prior.get('model_delivery') in ('success','degraded'):
            files={}
            for name,digest in prior.get('files',{}).items():
                file=Path(name);data=read_file(file.parent,file.name)
                if sha256(data)!=digest:raise ClosureError('prior delivery file changed')
                files[file.name]=data
            if files.get('FINAL.tex') or files.get('FINAL.md'):
                source=next(Path(n) for n in prior['files'] if Path(n).name in ('FINAL.tex','FINAL.md'))
                # Latest-writeup receipts carry the author save time as writeup_time_ns.
                saved=prior.get('captured_at_ns') or prior.get('writeup_time_ns')
                (candidates if saved else legacy_prior).append(dict(files=files,captured_at_ns=saved or source.stat().st_mtime_ns,
                    origin=prior.get('source_origin',str(source)),compilation=prior.get('compilation','pass' if files.get('FINAL.pdf') else 'not-reached'),
                    verification=prior.get('verification','completion-unconfirmed'),diagnostics=prior.get('diagnostics',''),checks=prior.get('checks')))
    for root in dict.fromkeys(map(str,roots)):
        path=Path(root)/'mixed-swarm/closure'
        if path.is_dir():candidates.extend(catalog_candidates(path,run))
        # Emergency releases use the same immutable receipt format. No directory
        # sweep into ledgers, worker scratch, credentials or unrelated runs.
        emergency=Path(root)/'deadline-delivery.json'
        if emergency.exists():
            receipt=json.loads(read_file(root,'deadline-delivery.json'))
            if receipt.get('run')!=run:raise ClosureError('foreign emergency delivery')
            if not receipt.get('placeholder'):
                files={}
                for name,digest in receipt['files'].items():
                    file=Path(name);data=read_file(file.parent,file.name)
                    if sha256(data)!=digest:raise ClosureError('emergency file changed')
                    files[file.name]=data
                candidates.append(dict(files=files,captured_at_ns=receipt['captured_at_ns'],
                    origin=receipt.get('source_origin'),compilation=receipt.get('compilation','not-reached'),
                    verification=receipt.get('verification','completion-unconfirmed'),diagnostics=receipt.get('diagnostics',''),checks=receipt.get('checks')))
    if not candidates:candidates=legacy_prior
    if not candidates:
        return release_files(run,{},destination,reason=reason,captured_at_ns=0)
    newest=max(candidates,key=latest_key)
    return release_files(run,newest['files'],destination,reason=reason,
        captured_at_ns=newest['captured_at_ns'],compilation=newest['compilation'],
        verification=newest['verification'],diagnostics=newest['diagnostics'],
        checks=newest['checks'],source_origin=newest['origin'])


def release_runtime(runtime, out, run, *, reason):
    """Closing exception backup: snapshot saved author files even without a build."""
    from .deadline_writeup import model_text
    from .closure_build import report_dependencies, BuildResult
    drafts=[]
    for w in [runtime.watchdog,*([runtime.backup] if getattr(runtime,'backup',None) else [])]:
        locations=[w.work]
        worker=getattr(w,'worker',None)
        if (worker is not None and getattr(worker,'role',None)=='report-author'
                and getattr(worker,'role_output',None) is not None):locations.append(Path(worker.role_output))
        for work in locations:
            for name in ('FINAL.tex','FINAL-DRAFT.tex','FINAL-REJECTED.tex','FINAL.md'):
                path=work/name
                if not path.exists():continue
                data=read_file(work,name)
                if not model_text(data):continue
                files={'FINAL.md' if name=='FINAL.md' else 'FINAL.tex':data}
                if name.endswith('.tex'):
                    try:
                        files=report_dependencies(work,main=name)
                        if name!='FINAL.tex':files['FINAL.tex']=files.pop(name)
                    except (OSError,ValueError):pass
                for extra in ('REPORT-CHECK.md','PROVENANCE.md'):
                    if (work/extra).exists():files[extra]=read_file(work,extra)
                # FINAL-DRAFT.tex / FINAL-REJECTED.tex hold bytes that failed the
                # report check; FINAL.tex with identical bytes failed it too.
                failed=name in ('FINAL-DRAFT.tex','FINAL-REJECTED.tex') or any(
                    (work/held).exists() and read_file(work,held)==data
                    for held in ('FINAL-DRAFT.tex','FINAL-REJECTED.tex'))
                drafts.append(dict(files=files,captured_at_ns=path.stat().st_mtime_ns,origin=str(path),failed=failed))
    if drafts:
        catalog=Path(out)/'mixed-swarm/closure'
        known=[];catalog_error=''
        if catalog.is_dir():
            try:known=catalog_candidates(catalog,run)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                catalog_error='Catalog lookup failed: '+type(exc).__name__+': '+str(exc)[:300]
        matched=set()
        for draft in drafts:
            ident=BuildResult(False,{k:v for k,v in draft['files'].items() if k!='REPORT-CHECK.md'},'',{}).candidate_sha256
            same=[c for c in known if c['candidate']==ident];matched.add(ident)
            # A work file is a copy: retained identical bytes keep the author save time,
            # and the catalog's build of these exact bytes reports its outcome.
            built=next((c for c in same if c['files'].get('FINAL.pdf')),same[-1] if same else {})
            if same:draft['captured_at_ns']=max(c['captured_at_ns'] for c in same)
            if built.get('files',{}).get('FINAL.pdf'):draft['files']['FINAL.pdf']=built['files']['FINAL.pdf']
            draft.update(compilation=built.get('compilation','not-reached'),
                verification=('report-check-failed; ' if draft.pop('failed') else '')+built.get('verification','completion-unconfirmed'),
                checks=runtime.watchdog.state.get('final_check_summary'),
                diagnostics='\n\n'.join(x for x in (built.get('diagnostics') or '',catalog_error) if x))
        # One ordering with the catalog: a newer retained draft beats an older work file.
        newest=max(drafts+[c for c in known if c['candidate'] not in matched],key=latest_key)
        receipt=release_files(run,newest['files'],Path(out)/'deadline-delivery',reason=reason,
            captured_at_ns=newest['captured_at_ns'],compilation=newest['compilation'],
            verification=newest['verification'],checks=newest['checks'],
            source_origin=newest['origin'],diagnostics=newest['diagnostics'])
    else:
        receipt=publish_latest(run,[out],Path(out)/'deadline-delivery',reason=reason)
    atomic_json(Path(out)/'deadline-delivery.json',receipt)
    return receipt
