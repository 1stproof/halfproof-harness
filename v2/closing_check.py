"""Closing-focused 25-minute check of the two swarm arms (research 8 min).

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

from .contracts_v3 import load_contracts, contracts_sha256, SWARM_CHECK, CLOSING_CHECK
from .control import atomic_json
from .deployment import DeploymentRefused, slurm_command
from .pilot import QUALIFICATION, _json_value
from .production import production_release, problem_record, validate_problem, route_layouts as production_layouts

ARMS = ('dsf-swarm', 'mixed-swarm')
PROBLEM = 2
CAP_MICROUSD = 120_000_000
CLOCKS = dict(research_stop_seconds=480, authoring_stop_seconds=3600, cleanup_stop_seconds=4020)
# 45-minute readiness + 60-minute wall + 7-minute cleanup, rounded up.
TARGET_SECONDS = 2 * 3600


def route_layouts(policy):
    """Production's swarm route ceilings; no solo reservations (no solos run)."""
    result={}
    for key,row in production_layouts(policy).items():
        limits={arm:n for arm,n in row['arm_limits'].items() if arm in ARMS}
        if not limits:continue
        result[key]=dict(capacity=row['capacity']-len(row['reserved_solos']),arm_limits=limits,reserved_solos=[])
    return result


def require_current_profile(policy):
    rows = policy['closing-check']
    if set(rows) != set(ARMS) or policy['closing-check_total_microusd'] != CAP_MICROUSD:
        raise DeploymentRefused('two-arm $80 swarm check required')
    for arm, row in rows.items():
        model = 'dsf' if arm == 'dsf-swarm' else 'k3'
        reserves = dict(row['closing_reserves_microusd'])
        if (row['cap_microusd'] != SWARM_CHECK['cap_microusd'][arm] or row['wall_seconds'] != 3600
                or row['research_stop_seconds'] != 480 or row['closing_max_seconds'] != 3120
                or row.get('orchestration') != 'stateless-v1' or row['orchestrator'] != model
                or row['final_author'] != model or row['revision_rounds'] != 1
                or reserves != SWARM_CHECK['closing_reserves_microusd'][arm]
                or sum(reserves.values()) != row['report_reserve_microusd']
                or sum(reserves.values()) >= row['cap_microusd']):
            raise DeploymentRefused('closing-check contract changed')


def prepare(release, *, cohort, durable_root, credential_file, nodes, partition='normal', arms=None):
    from .check_sizing import size
    live = list(ARMS) if not arms else list(arms)
    if not set(live) <= set(ARMS) or len(set(live)) != len(live):raise DeploymentRefused('unknown or repeated arm')
    path, pin = production_release(release)
    policy = load_contracts(Path(pin['release_root'])/'contracts')
    require_current_profile(policy)
    durable = Path(durable_root).absolute()
    if durable.exists() or any(p in ('runs-v2','web-harness-v2') for p in durable.parts):
        raise DeploymentRefused('new unique closing-check durable root required')
    manifest = dict(schema='v3-closing-check-v1', action='closing-check-cohort', mode='production', profile='closing-check',
        production_release=str(path), accepted_root=pin['accepted_root'], candidate_commit=pin['candidate_commit'],
        release_root=pin['release_root'], release_sha256=pin['release_sha256'], release_files=pin['release_files'],
        runtime_pin=pin['runtime_pin'], contracts_sha256=contracts_sha256(Path(pin['release_root'])/'contracts'),
        cohort_id=cohort, problem_id=f'b3-p{PROBLEM:02d}', problem=problem_record(PROBLEM), arms=list(ARMS),
        local_root='/tmp/v3-closing-check-'+cohort, durable_root=str(durable),
        credential_file=str(Path(credential_file).absolute()), fixtures=[], synthetic=False, fake_provider=False,
        cap_microusd=CAP_MICROUSD, infrastructure_contingency_microusd=0,
        arm_profiles=_json_value(policy['closing-check']), route_layouts=route_layouts(policy), clocks=dict(CLOCKS),
        live_qualification=_json_value(QUALIFICATION), withdrawal_authority='operator',
        target=dict(partition=partition,nodes=nodes,seconds=TARGET_SECONDS,**size(live)))
    if arms:manifest['live_arms']=sorted(live)
    validate(manifest)
    return manifest


def validate(manifest):
    if (manifest.get('schema') != 'v3-closing-check-v1' or manifest.get('action') != 'closing-check-cohort'
            or manifest.get('mode') != 'production' or manifest.get('profile') != 'closing-check'
            or sorted(manifest.get('arms',[])) != sorted(ARMS) or not set(manifest.get('live_arms',ARMS)) <= set(ARMS)
            or manifest.get('cap_microusd') != CAP_MICROUSD
            or manifest.get('infrastructure_contingency_microusd') != 0
            or not re.fullmatch('[A-Za-z0-9_-]{1,80}',manifest.get('cohort_id',''))):
        raise DeploymentRefused('fixed two-arm $80 closing-check configuration required')
    if any(manifest.get(k) for k in ('fixtures','synthetic','fake_provider','pacing','accelerated_clock','test_clock')):
        raise DeploymentRefused('swarm check cannot use offline fixtures or clocks')
    validate_problem(manifest['problem'])
    if manifest['problem'] != problem_record(PROBLEM) or manifest['problem_id'] != f'b3-p{PROBLEM:02d}':
        raise DeploymentRefused('swarm check runs problem 2 only')
    _, pin = production_release(manifest['production_release'])
    for key in ('accepted_root','candidate_commit','release_root','release_sha256','release_files','runtime_pin'):
        if manifest[key] != pin[key]:raise DeploymentRefused('release identity changed: '+key)
    policy = load_contracts(expected_sha256=manifest['contracts_sha256'])
    require_current_profile(policy)
    if manifest['arm_profiles'] != _json_value(policy['closing-check']) or manifest['route_layouts'] != route_layouts(policy):
        raise DeploymentRefused('closing-check profiles or capacity changed')
    if manifest['clocks'] != CLOCKS:raise DeploymentRefused('closing-check clocks changed')
    if manifest.get('live_qualification') != QUALIFICATION:
        raise DeploymentRefused('route qualification protocol changed')
    target = manifest['target']
    if (not target['nodes'] or not (target['nodes'] == ['*'] or set(target['nodes']) <= set(pin['target_nodes']))
            or target['partition'] != pin['target_partition'] or {k:target[k] for k in ('cpus','memory_gib')} != __import__('v2.check_sizing',fromlist=['x']).size(manifest.get('live_arms',list(ARMS)))
            or target['seconds'] != TARGET_SECONDS):
        raise DeploymentRefused('closing-check compute target changed')
    if manifest['local_root'] != '/tmp/v3-closing-check-'+manifest['cohort_id']:
        raise DeploymentRefused('closing-check scratch identity changed')
    if any(p in ('runs-v2','web-harness-v2') for p in Path(manifest['durable_root']).parts):
        raise DeploymentRefused('legacy paths prohibited')
    return True


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
    args = parser.parse_args(argv)
    output = Path(args.output).absolute()
    manifest = prepare(args.release,cohort=args.cohort,durable_root=args.durable_root,
        credential_file=args.credential_file,nodes=args.node,arms=[a for a in args.arms.split(',') if a] or None)
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
