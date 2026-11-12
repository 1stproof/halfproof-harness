"""No external measurement is imported or read by this writer."""
import argparse
import json
from pathlib import Path
import shutil
import time
from .core import *
from .runtime import Client, require_slurm, clean_tex, compile_tex
from .retained import collect_retained_candidates
from .safety import pinned_route, policy, retain, checkpoint, deliver


def roster_json(client,raw,name,schema,preferred=None):
    for retry in range(3):
        try:return parse(raw)
        except ValueError as exc:
            if retry==2:raise
            raw=client.own_call('Re-encode your own previous roster output as valid JSON. '
                'Preserve its mathematical content and conclusions. Do not add new mathematics. '
                'Required schema: '+schema,
                dict(previous_output=raw,format_error=str(exc)),name+'-json-'+str(retry),True,preferred=preferred)


def review_acceptance(review):
    if type(review.get('accept')) is not bool or not isinstance(review.get('statements'),list):
        raise ValueError('invalid per-statement roster referee audit')
    flags=('proof_complete','source_certified','needed_for_main','dependencies_complete')
    for row in review['statements']:
        if any(type(row.get(k)) is not bool for k in flags):
            raise ValueError('invalid roster statement verdict')
    if review['accept'] and (not review['statements'] or not all(all(row[k] for k in flags) for row in review['statements'])):
        review=dict(review,original_accept=True,accept=False,acceptance_consistency='own per-statement audit contains failures')
    return review


def audited_review(client,raw,packet,manuscript,compilation,name,reviewer,directory):
    for retry in range(3):
        try:return review_acceptance(parse(raw))
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            if retry==2:raise
            raw=client.own_call(REFEREE,dict(packet=packet,manuscript=manuscript,
                compilation=compilation,previous_review=raw,structural_error=str(exc)),
                name+'-audit-format-'+str(retry),True,preferred=reviewer)
            atomic_write(Path(directory)/('referee-audit-format-'+str(retry)+'.txt'),raw.encode())


def run(a):
    require_slurm()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
    contract=json.loads(Path(a.contract).read_text())
    manifest_raw=Path(a.manifest).read_bytes();manifest=json.loads(manifest_raw)
    original=Path(contract['problem']['source']).read_bytes()
    if digest(original)!=contract['problem']['sha256']: raise ValueError('problem hash mismatch')
    roster=contract['arm_profiles']['mixed-swarm']['models']
    routes=pinned_route(contract)
    rules=policy(contract)
    save(out/'writer-policy.json',rules)
    authority=json.loads(Path(a.authority).read_text())
    if authority['contract_sha256']!=digest(Path(a.contract).read_bytes()): raise ValueError('contract changed')
    if authority['manifest_sha256']!=digest(manifest_raw): raise ValueError('manifest changed')
    verified={(r['model'],r['provider']) for r in authority.get('routes',[]) if r.get('contract_route_allowed') is True}
    if not set(routes.values())<=verified: raise ValueError('unverified pinned route')
    atomic_write(out/'CLAIMS-MANIFEST.json',manifest_raw)
    atomic_write(out/'ORIGINAL-PROBLEM.tex',original)
    save(out/'authority.json',authority)
    source=dict(original_problem=original.decode(),certified_records=inventory(manifest))
    save(out/'source-packet.json',source)
    client=Client(contract,out/'calls',default_lane=getattr(a,'author','qwen24t'))
    result=dict(problem=a.problem,attempt=a.attempt,start=time.time(),status='started',compilation=None,
        preferred_author=getattr(a,'author','qwen24t'),preferred_referee=a.reviewer)
    candidates=[];plan={};selection={}
    recovery_origin=getattr(a,'candidates_from',None)
    if recovery_origin:
        origin_path=Path(recovery_origin)
        if (origin_path/'CLAIMS-MANIFEST.json').read_bytes()!=manifest_raw or (origin_path/'ORIGINAL-PROBLEM.tex').read_bytes()!=original:
            raise ValueError('recovery changed certified source or problem')
        from .control import recover
        candidates,plan,_=recover(recovery_origin)
        save(out/'plan.json',plan)
        checkpoint(out,candidates)
    try:
        candidate_origin=getattr(a,'candidates_from',None)
        origin_name=a.continue_from or candidate_origin or getattr(a,'plan_from',None)
        origin=Path(origin_name) if origin_name else None
        previous=origin if (a.continue_from or candidate_origin) else None
        if origin:
            if (origin/'CLAIMS-MANIFEST.json').read_bytes()!=manifest_raw:
                raise ValueError('continuation changed certified source')
            if (origin/'ORIGINAL-PROBLEM.tex').read_bytes()!=original:
                raise ValueError('continuation changed original problem')
            raw=(origin/'plan.json').read_text()
            if candidate_origin:
                retained=collect_retained_candidates(origin)
                choice=roster_json(client,client.own_call(SELECT+'\nSelect an input for further writing only. This is not a delivery or proof certification.',
                    dict(source=source,plan=raw,candidates=retained),'input-selection',True),
                    'input-selection','{candidate:integer,reason:string,paper_like:boolean}')
                k=choice['candidate']
                if type(k) is not int or not 0<=k<len(retained):raise ValueError('invalid roster input selection')
                prior=retained[k]['manuscript']
                save(out/'input-selection.json',dict(**choice,origin=str(origin),sha256=retained[k]['sha256']))
            else:prior=(previous/'FINAL.tex').read_text() if previous else None
            save(out/'continuation.json',dict(previous=str(origin),prior_sha256=digest(prior.encode()) if prior else None,
                mode='retained-candidates' if candidate_origin else ('manuscript' if previous else 'plan-only'),plan_sha256=digest(raw.encode()),
                input_policy='own manuscript and plan only; no external measurement inputs',reviewer=a.reviewer))
        else:
            raw=client.own_call(PLAN,source,'plan',True)
        atomic_write(out/'plan-raw.txt',raw.encode())
        for i in range(3):
            try:
                plan=validate_plan(parse(raw),manifest);break
            except (ValueError,KeyError,TypeError) as exc:
                if i==2: raise
                raw=client.own_call(PLAN,dict(source=source,previous_plan=raw,
                    structural_error=str(exc)),'plan-format-'+str(i),True)
                atomic_write(out/('plan-raw-'+str(i+1)+'.txt'),raw.encode())
        save(out/'plan.json',plan)
        selected_records=[c for c in inventory(manifest) if c['current_record'] in plan['included_records']]
        packet=dict(original_problem=source['original_problem'],plan=plan,certified_records=selected_records)
        save(out/'draft-packet.json',packet)
        if previous:
            seed_raw=client.own_call(REFEREE,dict(source=source,plan=plan,manuscript=prior),
                'fresh-roster-referee',True,preferred=a.reviewer)
            atomic_write(out/'fresh-roster-referee.txt',seed_raw.encode())
            # This stage needs the roster's feedback, not a machine decision.
            # Preserve its exact prose even when it ignores the JSON format.
            seed_review=seed_raw
            replanned=client.own_call(PLAN,dict(source=source,previous_plan=plan,roster_review=seed_review),
                'fresh-roster-plan',True)
            atomic_write(out/'fresh-roster-plan.txt',replanned.encode())
            plan=validate_plan(roster_json(client,replanned,'fresh-plan',PLAN),manifest)
            save(out/'plan.json',plan)
            packet=dict(original_problem=source['original_problem'],plan=plan,
                certified_records=[c for c in inventory(manifest) if c['current_record'] in plan['included_records']])
            save(out/'draft-packet.json',packet)
            raw=client.own_call(DRAFT+'\nReturn a complete new edition of your own prior paper using this fresh roster review.',
                dict(packet=packet,previous_manuscript=prior,roster_review=seed_review),'draft')
        else:
            raw=client.own_call(DRAFT,packet,'draft')
        for cycle in range(rules['revisions']+1):
            candidate_dir=out/('candidate-'+str(cycle));candidate_dir.mkdir()
            atomic_write(candidate_dir/'raw.txt',raw.encode())
            for repair in range(rules['repair_rounds']+1):
                try:
                    manuscript=clean_tex(raw)
                except ValueError as exc:
                    if repair==rules['repair_rounds']: raise
                    raw=client.own_call(DRAFT,dict(packet=packet,previous=raw,format_error=str(exc)),
                        'format-'+str(cycle)+'-'+str(repair))
                    continue
                c=retain(out,manuscript,candidate_dir,dict(ok=False,errors=['compile pending']),
                    plan['included_records'],candidates)
                comp=compile_tex(manuscript,candidate_dir)
                c['compilation']=comp;checkpoint(out,candidates)
                deliver(out,candidates,plan,flags=['writing_in_progress'],
                    contract_hash=digest(Path(a.contract).read_bytes()))
                if (comp['ok'] and not c['violations']) or repair==rules['repair_rounds']: break
                raw=client.own_call(DRAFT+'\nRepair the reported LaTeX and statement-binding diagnostics. Only you may edit the manuscript.',
                    dict(packet=packet,manuscript=manuscript,compiler=comp,
                         statement_diagnostics=c['violations']),
                    'compile-'+str(cycle)+'-'+str(repair))
                candidate_dir=out/('candidate-'+str(cycle)+'-fix-'+str(repair))
            review_raw=client.own_call(REFEREE,dict(packet=packet,manuscript=manuscript,compilation=comp),
                'referee-'+str(cycle),True,preferred=a.reviewer)
            atomic_write(candidate_dir/'referee.txt',review_raw.encode())
            review=audited_review(client,review_raw,packet,manuscript,comp,'referee-'+str(cycle),a.reviewer,candidate_dir)
            save(candidate_dir/'referee.json',review)
            c['review']=review;checkpoint(out,candidates)
            if (review['accept'] and comp['ok'] and not c['violations']) or cycle==rules['revisions']: break
            raw=client.own_call(DRAFT+'\nRevise using only your own roster referee. Return the complete paper.',
                dict(packet=packet,manuscript=manuscript,referee=review,compilation=comp),
                'revision-'+str(cycle+1))
        selection=roster_json(client,client.own_call(SELECT,dict(plan=plan,candidates=candidates),'selection',True),
            'selection','{candidate:integer,reason:string,paper_like:boolean}')
        chosen=selection['candidate']
        if type(chosen) is not int or not 0<=chosen<len(candidates): raise ValueError('invalid editor selection')
        c=candidates[chosen]
        save(out/'selection.json',dict(**selection,sha256=c['sha256']))
        result.update(status='finished',compilation=c['compilation'],final_sha256=c['sha256'],
            roster_paper_like=selection.get('paper_like'),selected_candidate=chosen,
            main_records=plan['main_records'],included_records=plan['included_records'])
    except Exception as exc:
        result.update(status='unfinished',error=type(exc).__name__+': '+str(exc),retained_candidates=len(candidates))
    delivered=deliver(out,candidates,plan,selection.get('candidate'),
        flags=([result['error']] if result.get('error') else []),
        contract_hash=digest(Path(a.contract).read_bytes()))
    result['delivery']=delivered
    result.update(compilation=delivered['compilation'],selected_candidate=delivered['selected_candidate'],
        final_sha256=delivered['hashes']['paper_tex'])
    result.update(end=time.time(),original_manifest_unchanged=digest(Path(a.manifest).read_bytes())==digest(manifest_raw),
        original_problem_unchanged=digest(Path(contract['problem']['source']).read_bytes())==digest(original))
    save(out/'writing-result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='compilation'}),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ('out','contract','manifest','authority','problem'): p.add_argument('--'+key,required=True)
    p.add_argument('--single-attempt',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--attempt',type=int,default=1)
    p.add_argument('--reviewer',choices=tuple(ROUTES),default='dsf')
    p.add_argument('--author',choices=tuple(ROUTES),default='qwen24t')
    continuation=p.add_mutually_exclusive_group()
    continuation.add_argument('--continue-from')
    continuation.add_argument('--plan-from')
    continuation.add_argument('--candidates-from')
    args=p.parse_args()
    if args.single_attempt:run(args)
    else:
        from .control import schedule
        schedule(args)
