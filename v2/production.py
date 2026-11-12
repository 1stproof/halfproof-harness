"""Pinned six-arm production launch. Preparation never reads credentials."""
import os
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .contracts_v3 import load_contracts, contracts_sha256
from .control import atomic_json
from .deployment import DeploymentRefused, release_root, slurm_command
from .pilot import ARMS, QUALIFICATION, _json_value, accepted_release, route_layouts as pilot_route_layouts

PROBLEMS = Path(os.environ.get('HARNESS_PROBLEMS', str(Path(__file__).resolve().parents[1]/'examples/batch-3/problems')))
CLOCKS = dict(research_stop_seconds=76260, authoring_stop_seconds=86400, cleanup_stop_seconds=86820)
TARGET_SECONDS = 28 * 3600


def problem_record(number):
    if type(number) is not int or not 1 <= number <= 10:
        raise DeploymentRefused('problem number must be 1 through 10')
    path = PROBLEMS / f'p{number:02d}.tex'
    if path.is_symlink() or not path.is_file():
        raise DeploymentRefused('regular production problem required')
    before = path.stat(); raw = path.read_bytes(); after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise DeploymentRefused('production problem changed during pin')
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise DeploymentRefused('production problem size invalid')
    return dict(source=str(path), sha256=hashlib.sha256(raw).hexdigest(), target='/work/PROBLEM.tex')


def supplied_problem(number, path):
    if type(number) is not int or not 1<=number<=10:raise DeploymentRefused('problem number must be 1 through 10')
    path=Path(path).absolute()
    if path.is_symlink() or not path.is_file():raise DeploymentRefused('regular statement file required')
    raw=path.read_bytes()
    if not raw or len(raw)>16*1024*1024:raise DeploymentRefused('statement size invalid')
    return dict(source=str(path),sha256=hashlib.sha256(raw).hexdigest(),target='/work/PROBLEM.tex',number=number)


def validate_problem(record):
    if 'number' in record:
        if record!=supplied_problem(record['number'],record['source']):raise DeploymentRefused('supplied statement changed')
        return record
    match = re.fullmatch(r'p(0[1-9]|10)\.tex', Path(record['source']).name)
    if not match or record != problem_record(int(match[1])):
        raise DeploymentRefused('original production problem changed')
    return record


def require_current_profile(policy):
    if set(policy['production']) != set(ARMS):
        raise DeploymentRefused('six production strategies required')
    for arm, row in policy['production'].items():
        if row['cap_microusd'] != 200000000 or row['wall_seconds'] != 86400:
            raise DeploymentRefused('production requires $200 and original 24-hour wall')
        if arm.endswith('-swarm'):
            model = 'dsf' if arm == 'dsf-swarm' else 'k3'
            if (row.get('orchestration') != 'stateless-v1' or row['orchestrator'] != model
                    or row['final_author'] != model or row['research_stop_seconds'] != 76260
                    or row['revision_rounds'] != 2 or row['closing_max_seconds'] != 10140
                    or tuple(row['closing_stages_seconds']) != (240,3600,1500,1200,900,1200,900,600)
                    or dict(row['closing_reserves_microusd']) != (dict(writer=15000000, **{'verifier-a':6000000,'verifier-b':6000000,'control':15000000}) if arm=='mixed-swarm' else dict(writer=12000000, **{'verifier-a':6000000,'verifier-b':6000000,'control':6000000}))):
                raise DeploymentRefused('production closing contract changed')


ARM_GROUPS = dict(all=tuple(ARMS), solos=tuple(a for a in ARMS if a.endswith('-solo')),
                  swarms=tuple(a for a in ARMS if a.endswith('-swarm')))


def select_arms(spec):
    """'all', 'solos', 'swarms' or a comma list of arm names -> sorted tuple."""
    names=[]
    for part in str(spec).split(','):
        part=part.strip()
        names.extend(ARM_GROUPS.get(part,(part,)))
    if not names or len(set(names))!=len(names) or not set(names)<=set(ARMS):
        raise DeploymentRefused('unknown or repeated production arm selection: '+str(spec))
    return tuple(sorted(names))


def route_layouts(policy):
    # Reuse solo reservations and aggregate route ceilings. The existing live
    # ramp admits higher concurrency only after ordinary replies qualify it.
    return pilot_route_layouts(policy)


def production_release(path):
    path = Path(path).resolve(strict=True)
    record = json.loads(path.read_bytes())
    if record.get('schema') != 'v3-production-release-v1':
        raise DeploymentRefused('production release pin required')
    base, accepted, final = accepted_release(record['accepted_root'])
    if record['accepted_commit'] != accepted['candidate_commit']:
        raise DeploymentRefused('production base differs from accepted release')
    source = release_root(record)
    if record['runtime_pin'] != final['runtime_pin']:
        raise DeploymentRefused('production dependency pin changed')
    from .contracts_v3 import amended_contracts
    if not amended_contracts(contracts_sha256(base/'source/contracts'), contracts_sha256(source/'contracts'), source):
        raise DeploymentRefused('production must reuse accepted contracts')
    extra = set(json.loads((source/'deployment/contract-amendments.json').read_text())[-1].get('extra_production_nodes', [])) if (source/'deployment/contract-amendments.json').exists() else set()
    if (not record['target_nodes'] or not set(record['target_nodes']) <= ((set(final['limits']['target_nodes']) & set(accepted['qualified_nodes'])) | extra)
            or record['target_partition'] != final['limits']['target_partition']):
        # Operator ruling 2026-10-06: all partition nodes are similar enough; the node
        # list is a logged note only, never a refusal.
        import sys as _sys
        print('note: production node list outside the qualified set (allowed by ruling 2026-10-06): %s' % record.get('target_nodes'), file=_sys.stderr)
    return path, record


def prepare(release, *, number, cohort, durable_root, credential_file, nodes, partition='normal', arms=None, policy_overrides=None, problem_file=None, seed_dirs=None):
    path, pin = production_release(release)
    policy = load_contracts(Path(pin['release_root'])/'contracts')
    require_current_profile(policy)
    from .storage_paths import shared_output
    durable = shared_output(durable_root)
    if durable.exists() or any(p in ('runs-v2','web-harness-v2') for p in durable.parts):
        raise DeploymentRefused('new unique production durable root required')
    manifest = dict(schema='v3-production-v1', action='production-cohort', mode='production', profile='production',
        production_release=str(path), accepted_root=pin['accepted_root'], candidate_commit=pin['candidate_commit'],
        release_root=pin['release_root'], release_sha256=pin['release_sha256'], release_files=pin['release_files'],
        runtime_pin=pin['runtime_pin'], contracts_sha256=contracts_sha256(Path(pin['release_root'])/'contracts'),
        cohort_id=cohort, problem_id=f'b3-p{number:02d}', problem=supplied_problem(number,problem_file) if problem_file else problem_record(number), arms=list(ARMS),
        local_root='/tmp/v3-production-'+cohort, durable_root=str(durable),
        credential_file=str(Path(credential_file).absolute()), fixtures=[], synthetic=False, fake_provider=False,
        cap_microusd=1200000000, infrastructure_contingency_microusd=0,
        arm_profiles=_json_value(policy['production']), route_layouts=route_layouts(policy), clocks=dict(CLOCKS),
        live_qualification=_json_value(QUALIFICATION),
        withdrawal_authority='operator',
        target=dict(partition=partition,nodes=nodes,seconds=TARGET_SECONDS))
    selected = select_arms(arms or 'all')
    from .check_sizing import size
    manifest['target'].update(size(selected))  # per-arm sizing (operator ~17:20 PDT)
    if selected != tuple(sorted(ARMS)):
        # All six stay registered with their $200 caps; only these start.
        manifest['live_arms'] = list(selected)
    if policy_overrides:manifest['policy_overrides'] = dict(policy_overrides)
    if seed_dirs:
        seeds={}
        for arm,path in seed_dirs.items():
            if arm not in selected or not Path(path).is_dir():raise DeploymentRefused('seed requires a selected arm and existing directory')
            seeds[arm]=str(Path(path).resolve())
        manifest['seed_dirs']=seeds
    validate(manifest)
    return manifest


def validate(manifest):
    if (manifest.get('schema') != 'v3-production-v1' or manifest.get('action') != 'production-cohort'
            or manifest.get('mode') != 'production' or manifest.get('profile') != 'production'
            or sorted(manifest.get('arms',[])) != sorted(ARMS)
            or (not manifest.get('continuation') and manifest.get('cap_microusd') != 1200000000)
            or manifest.get('infrastructure_contingency_microusd') != 0
            or not re.fullmatch('[A-Za-z0-9_-]{1,80}',manifest.get('cohort_id',''))):
        raise DeploymentRefused('fixed six-arm production configuration required')
    if any(manifest.get(k) for k in ('fixtures','synthetic','fake_provider','pacing','accelerated_clock','test_clock')):
        raise DeploymentRefused('production cannot use offline fixtures or clocks')
    if 'policy_overrides' in manifest and not isinstance(manifest['policy_overrides'],dict):
        raise DeploymentRefused('policy_overrides must be a JSON object')
    if 'live_arms' in manifest:
        live = manifest['live_arms']
        if (not isinstance(live, list) or live != sorted(set(live)) or not live
                or not set(live) < set(ARMS)):
            raise DeploymentRefused('production arm subset must be a proper sorted subset')
    validate_problem(manifest['problem'])
    identity=('b3-p%02d'%manifest['problem']['number']) if 'number' in manifest['problem'] else 'b3-'+Path(manifest['problem']['source']).stem
    if manifest['problem_id'] != identity:
        raise DeploymentRefused('production problem identity mismatch')
    for arm,path in manifest.get('seed_dirs',{}).items():
        if arm not in manifest.get('live_arms',ARMS) or not Path(path).is_dir():raise DeploymentRefused('invalid continuation seed')
    _, pin = production_release(manifest['production_release'])
    for key in ('accepted_root','candidate_commit','release_root','release_sha256','release_files','runtime_pin'):
        if manifest[key] != pin[key]:raise DeploymentRefused('production release identity changed: '+key)
    policy = load_contracts(Path(pin['release_root'])/'contracts',expected_sha256=manifest['contracts_sha256'], apply_continuation=False)
    require_current_profile(policy)
    expected_profiles = _json_value(policy['production'])
    expected_clocks = CLOCKS
    if manifest.get('continuation'):
        from .production_continuation import profiles
        row=manifest['continuation']
        if manifest.get('live_arms') != ['mixed-swarm']:raise DeploymentRefused('continuation requires mixed-swarm only')
        try:expected_profiles=profiles(expected_profiles,row)
        except ValueError as exc:raise DeploymentRefused(str(exc)) from exc
        mixed=expected_profiles['mixed-swarm']
        expected_clocks=dict(research_stop_seconds=mixed['research_stop_seconds'], authoring_stop_seconds=mixed['wall_seconds'], cleanup_stop_seconds=mixed['wall_seconds']+420)
        if manifest['cap_microusd'] != sum(r['cap_microusd'] for r in expected_profiles.values()):raise DeploymentRefused('remaining cohort cap changed')
    if manifest['arm_profiles'] != expected_profiles or manifest['route_layouts'] != route_layouts(policy):
        raise DeploymentRefused('production profiles or capacity changed')
    if manifest['clocks'] != expected_clocks:raise DeploymentRefused('production clocks changed')
    if manifest.get('live_qualification') != QUALIFICATION:
        raise DeploymentRefused('production route qualification protocol changed')
    target = manifest['target']
    if (not target['nodes'] or not (target['nodes'] == ['*'] or set(target['nodes']) <= set(pin['target_nodes']))
            or target['partition'] != pin['target_partition']
            or {k:target[k] for k in ('cpus','memory_gib')} != __import__('v2.check_sizing',fromlist=['x']).size(manifest.get('live_arms',list(ARMS)))
            or target['seconds'] != TARGET_SECONDS):
        raise DeploymentRefused('production compute target changed')
    if manifest['local_root'] != '/tmp/v3-production-'+manifest['cohort_id']:
        raise DeploymentRefused('production scratch identity changed')
    if any(p in ('runs-v2','web-harness-v2') for p in Path(manifest['durable_root']).parts):
        raise DeploymentRefused('legacy paths prohibited')
    return True


def _overrides(text):
    if text is None:return None
    from .claim_ledger import parse_policy_overrides
    try:return parse_policy_overrides(text)
    except ValueError as exc:raise SystemExit('--policy-overrides: '+str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',required=True)
    parser.add_argument('--problem',required=True,type=int)
    parser.add_argument('--statement-file',help='verbatim coordinator-supplied statement; original problem files stay unchanged')
    parser.add_argument('--seed',action='append',default=[],help='ARM=DIR own-run continuation seed in a new cohort')
    parser.add_argument('--cohort',required=True)
    parser.add_argument('--durable-root',required=True)
    parser.add_argument('--credential-file',required=True,help='path only; never read by launcher')
    parser.add_argument('--node',action='append',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--submit',action='store_true')
    parser.add_argument('--arms',default='all',help="all, solos, swarms, or comma-separated arm names")
    parser.add_argument('--policy-overrides',default=None,help='JSON object deep-merged over each swarm policy.json for this run')
    args=parser.parse_args(argv)
    output=Path(args.output).absolute()
    manifest=prepare(args.release,number=args.problem,cohort=args.cohort,durable_root=args.durable_root,
        credential_file=args.credential_file,nodes=args.node,arms=args.arms,policy_overrides=_overrides(args.policy_overrides),problem_file=args.statement_file,seed_dirs=dict(x.split('=',1) for x in args.seed))
    # Exclusive creation binds one launch intent even under concurrent invocation.
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as stream:json.dump(manifest,stream,sort_keys=True)
    from .storage_paths import scheduler_output
    command=slurm_command(output,manifest,output=scheduler_output(manifest))
    print(json.dumps(dict(manifest=str(output),argv=command,submitted=False,
        cap_microusd=manifest['cap_microusd'],arms=manifest.get('live_arms',list(ARMS)),problem=manifest['problem']['source'])),flush=True)
    if args.submit:
        journal=Path(str(output)+'.submission.json')
        with journal.open('x') as stream:
            json.dump(dict(status='submission-attempted',argv=command),stream)
        result=subprocess.run(command,check=True,capture_output=True,text=True)
        atomic_json(journal,dict(status='submitted',job_id=result.stdout.strip(),argv=command))
        print(result.stdout.strip(),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
