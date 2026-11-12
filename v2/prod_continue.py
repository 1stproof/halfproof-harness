"""Continuation of the production swarms (13.5 h wall, production stages, seeded with prior work).

Same release, route preflight, deferred credential, qualification ramp and
closing machinery as production; only the derived swarm-check profile differs:
research stops at 45 minutes, closing (reserves inside each $20) ends by 60.
Preparation never reads credentials.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

from .contracts_v3 import load_contracts, contracts_sha256, PROD_CONTINUE_WALL, PROD_CONTINUE_WALLS, PROD_CONTINUE_CLOSING_STAGES
from .control import atomic_json
from .deployment import DeploymentRefused, slurm_command
from .pilot import QUALIFICATION, _json_value
from .production import production_release, problem_record, validate_problem, route_layouts as production_layouts

ARMS = ('dsf-swarm', 'mixed-swarm')
PROBLEM = 2
CAP_MICROUSD = 400_000_000
LONGEST = max(PROD_CONTINUE_WALLS.values())
CLOSING = sum(PROD_CONTINUE_CLOSING_STAGES)
CLOCKS = dict(research_stop_seconds=LONGEST-CLOSING, authoring_stop_seconds=LONGEST, cleanup_stop_seconds=LONGEST+420)
# 45-minute readiness + 60-minute wall + 7-minute cleanup, rounded up.
TARGET_SECONDS = LONGEST + 2 * 3600


def route_layouts(policy):
    """Production's swarm route ceilings; no solo reservations (no solos run)."""
    result={}
    for key,row in production_layouts(policy).items():
        limits={arm:n for arm,n in row['arm_limits'].items() if arm in ARMS}
        if not limits:continue
        result[key]=dict(capacity=row['capacity']-len(row['reserved_solos']),arm_limits=limits,reserved_solos=[])
    return result


def require_current_profile(policy):
    rows = policy['prod-continue']
    if set(rows) != set(ARMS) or policy['prod-continue_total_microusd'] != CAP_MICROUSD:
        raise DeploymentRefused('two-arm $80 swarm check required')
    for arm, row in rows.items():
        model = 'dsf' if arm == 'dsf-swarm' else 'k3'
        reserves = dict(row['closing_reserves_microusd'])
        if (row['cap_microusd'] != 200_000_000 or row['wall_seconds'] != PROD_CONTINUE_WALLS[arm]
                or row['closing_max_seconds'] != CLOSING or tuple(row['closing_stages_seconds']) != PROD_CONTINUE_CLOSING_STAGES
                or row.get('orchestration') != 'stateless-v1' or row['orchestrator'] != model
                or row['final_author'] != model or row['revision_rounds'] != 2
                or sum(reserves.values()) != row['report_reserve_microusd']
                or sum(reserves.values()) >= row['cap_microusd']):
            raise DeploymentRefused('prod-continue contract changed')


def prepare(release, *, cohort, durable_root, credential_file, nodes, partition='normal', arms=None, seed_dirs=None, policy_overrides=None, problem=PROBLEM):
    from .check_sizing import size
    live = list(ARMS) if not arms else list(arms)
    if not set(live) <= set(ARMS) or len(set(live)) != len(live):raise DeploymentRefused('unknown or repeated arm')
    path, pin = production_release(release)
    policy = load_contracts(Path(pin['release_root'])/'contracts')
    require_current_profile(policy)
    durable = Path(durable_root).absolute()
    if durable.exists() or any(p in ('runs-v2','web-harness-v2') for p in durable.parts):
        raise DeploymentRefused('new unique prod-continue durable root required')
    manifest = dict(schema='v3-prod-continue-v1', action='prod-continue-cohort', mode='production', profile='prod-continue',
        production_release=str(path), accepted_root=pin['accepted_root'], candidate_commit=pin['candidate_commit'],
        release_root=pin['release_root'], release_sha256=pin['release_sha256'], release_files=pin['release_files'],
        runtime_pin=pin['runtime_pin'], contracts_sha256=contracts_sha256(Path(pin['release_root'])/'contracts'),
        cohort_id=cohort, problem_id=f'b3-p{problem:02d}', problem=problem_record(problem), arms=list(ARMS),
        local_root='/tmp/v3-prod-continue-'+cohort, durable_root=str(durable),
        credential_file=str(Path(credential_file).absolute()), fixtures=[], synthetic=False, fake_provider=False,
        cap_microusd=CAP_MICROUSD, infrastructure_contingency_microusd=0,
        arm_profiles=_json_value(policy['prod-continue']), route_layouts=route_layouts(policy), clocks=dict(CLOCKS),
        live_qualification=_json_value(QUALIFICATION), withdrawal_authority='operator',
        target=dict(partition=partition,nodes=nodes,seconds=TARGET_SECONDS,**size(live)))
    if arms:manifest['live_arms']=sorted(live)
    if seed_dirs:manifest['seed_dirs']={a:str(Path(p).resolve()) for a,p in seed_dirs.items() if Path(p).is_dir()}
    if policy_overrides:manifest['policy_overrides']=dict(policy_overrides)
    validate(manifest)
    return manifest


def validate(manifest):
    if (manifest.get('schema') != 'v3-prod-continue-v1' or manifest.get('action') != 'prod-continue-cohort'
            or manifest.get('mode') != 'production' or manifest.get('profile') != 'prod-continue'
            or sorted(manifest.get('arms',[])) != sorted(ARMS) or not set(manifest.get('live_arms',ARMS)) <= set(ARMS)
            or manifest.get('cap_microusd') != CAP_MICROUSD
            or manifest.get('infrastructure_contingency_microusd') != 0
            or not re.fullmatch('[A-Za-z0-9_-]{1,80}',manifest.get('cohort_id',''))):
        raise DeploymentRefused('fixed two-arm $80 prod-continue configuration required')
    if any(manifest.get(k) for k in ('fixtures','synthetic','fake_provider','pacing','accelerated_clock','test_clock')):
        raise DeploymentRefused('swarm check cannot use offline fixtures or clocks')
    if 'policy_overrides' in manifest and not isinstance(manifest['policy_overrides'],dict):
        raise DeploymentRefused('policy_overrides must be a JSON object')
    validate_problem(manifest['problem'])
    if manifest['problem_id'] != 'b3-'+Path(manifest['problem']['source']).stem:
        raise DeploymentRefused('prod-continue problem identity mismatch')
    _, pin = production_release(manifest['production_release'])
    for key in ('accepted_root','candidate_commit','release_root','release_sha256','release_files','runtime_pin'):
        if manifest[key] != pin[key]:raise DeploymentRefused('release identity changed: '+key)
    policy = load_contracts(Path(pin['release_root'])/'contracts',expected_sha256=manifest['contracts_sha256'])
    require_current_profile(policy)
    if manifest['arm_profiles'] != _json_value(policy['prod-continue']) or manifest['route_layouts'] != route_layouts(policy):
        raise DeploymentRefused('prod-continue profiles or capacity changed')
    if manifest['clocks'] != CLOCKS:raise DeploymentRefused('prod-continue clocks changed')
    if manifest.get('live_qualification') != QUALIFICATION:
        raise DeploymentRefused('route qualification protocol changed')
    target = manifest['target']
    if (not target['nodes'] or not (target['nodes'] == ['*'] or set(target['nodes']) <= set(pin['target_nodes']))
            or target['partition'] != pin['target_partition'] or {k:target[k] for k in ('cpus','memory_gib')} != __import__('v2.check_sizing',fromlist=['x']).size(manifest.get('live_arms',list(ARMS)))
            or target['seconds'] != TARGET_SECONDS):
        raise DeploymentRefused('prod-continue compute target changed')
    if manifest['local_root'] != '/tmp/v3-prod-continue-'+manifest['cohort_id']:
        raise DeploymentRefused('prod-continue scratch identity changed')
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
    parser.add_argument('--cohort',required=True)
    parser.add_argument('--durable-root',required=True)
    parser.add_argument('--credential-file',required=True,help='path only; never read by launcher')
    parser.add_argument('--node',action='append',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--submit',action='store_true')
    parser.add_argument('--arms',default='',help='comma-separated subset; default all')
    parser.add_argument('--seed',action='append',default=[],help='ARM=DIR continuation seed')
    parser.add_argument('--problem',type=int,default=PROBLEM,help='batch-3 problem number (1-10)')
    parser.add_argument('--policy-overrides',default=None,help='JSON object deep-merged over each swarm policy.json for this run')
    args = parser.parse_args(argv)
    output = Path(args.output).absolute()
    manifest = prepare(args.release,cohort=args.cohort,durable_root=args.durable_root,
        credential_file=args.credential_file,nodes=args.node,arms=[a for a in args.arms.split(',') if a] or None,seed_dirs=dict(x.split('=',1) for x in args.seed),policy_overrides=_overrides(args.policy_overrides),problem=args.problem)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as stream:json.dump(manifest,stream,sort_keys=True)
    command = slurm_command(output,manifest,output=str(output)+'.slurm.log')
    print(json.dumps(dict(manifest=str(output),argv=command,submitted=False,cap_microusd=CAP_MICROUSD,
        arms=list(ARMS),problem=manifest['problem']['source'])),flush=True)
    if args.submit:
        journal = Path(str(output)+'.submission.json')
        with journal.open('x') as stream:
            json.dump(dict(status='submission-attempted',argv=command),stream)
        result = subprocess.run(command,check=True,capture_output=True,text=True)
        atomic_json(journal,dict(status='submitted',job_id=result.stdout.strip(),argv=command))
        print(result.stdout.strip(),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
