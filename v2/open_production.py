"""Open swarm (open-production): no orchestrator; members dsf, dsp, k3, qwen24t; glm light member.

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

from .contracts_v3 import load_contracts, contracts_sha256
from .control import atomic_json
from .deployment import DeploymentRefused, slurm_command
from .pilot import QUALIFICATION, _json_value
from .production import production_release, problem_record, validate_problem, route_layouts as production_layouts

ARMS = ('open-swarm',)
PROFILE = 'open-production'
PROBLEM = 2
CAP_MICROUSD = 200000000
CLOCKS = dict(research_stop_seconds=76260, authoring_stop_seconds=86400, cleanup_stop_seconds=86820)
# 45-minute readiness + 60-minute wall + 7-minute cleanup, rounded up.
TARGET_SECONDS = 28 * 3600
CONTINUE_WALL = 10260
PROFILES = {'open-production': dict(clocks=CLOCKS, seconds=TARGET_SECONDS, wall=86400),
    'open-continue': dict(clocks=dict(research_stop_seconds=CONTINUE_WALL-10140, authoring_stop_seconds=CONTINUE_WALL,
        cleanup_stop_seconds=CONTINUE_WALL+420), seconds=CONTINUE_WALL+2*3600, wall=CONTINUE_WALL)}


def route_layouts(policy, profile=PROFILE):
    """Every member model routes to the one open-swarm arm; no concurrency limits apply."""
    row = policy[profile]['open-swarm']
    return {key: dict(capacity=8, arm_limits={'open-swarm': 8}, reserved_solos=[]) for key in row['models']}


def require_current_profile(policy, profile=PROFILE):
    rows = policy[profile]
    if set(rows) != set(ARMS) or policy[profile+'_total_microusd'] != CAP_MICROUSD:
        raise DeploymentRefused('open-swarm profile required')
    row = rows['open-swarm']
    if (row['cap_microusd'] != CAP_MICROUSD or row['wall_seconds'] != PROFILES[profile]['wall']
            or row['research_stop_seconds'] != PROFILES[profile]['clocks']['research_stop_seconds']
            or row['closing_max_seconds'] != 10140 or not row.get('open_swarm')):
        raise DeploymentRefused('open-swarm contract changed')


def prepare(release, *, cohort, durable_root, credential_file, nodes, partition='normal', arms=None, profile=PROFILE, seed_dirs=None, policy_overrides=None, problem=PROBLEM):
    from .check_sizing import size
    live = list(ARMS) if not arms else list(arms)
    if not set(live) <= set(ARMS) or len(set(live)) != len(live):raise DeploymentRefused('unknown or repeated arm')
    path, pin = production_release(release)
    policy = load_contracts(Path(pin['release_root'])/'contracts')
    require_current_profile(policy, profile)
    durable = Path(durable_root).absolute()
    if durable.exists() or any(p in ('runs-v2','web-harness-v2') for p in durable.parts):
        raise DeploymentRefused('new unique open-production durable root required')
    manifest = dict(schema='v3-open-production-v1', action='open-production-cohort', mode='production', profile=profile,
        production_release=str(path), accepted_root=pin['accepted_root'], candidate_commit=pin['candidate_commit'],
        release_root=pin['release_root'], release_sha256=pin['release_sha256'], release_files=pin['release_files'],
        runtime_pin=pin['runtime_pin'], contracts_sha256=contracts_sha256(Path(pin['release_root'])/'contracts'),
        cohort_id=cohort, problem_id=f'b3-p{problem:02d}', problem=problem_record(problem), arms=list(ARMS),
        local_root='/tmp/v3-'+profile+'-'+cohort, durable_root=str(durable),
        credential_file=str(Path(credential_file).absolute()), fixtures=[], synthetic=False, fake_provider=False,
        cap_microusd=CAP_MICROUSD, infrastructure_contingency_microusd=0,
        arm_profiles=_json_value(policy[profile]), route_layouts=route_layouts(policy, profile), clocks=dict(PROFILES[profile]['clocks']),
        live_qualification=_json_value(QUALIFICATION), withdrawal_authority='operator',
        target=dict(partition=partition,nodes=nodes,seconds=PROFILES[profile]['seconds'],**size(live)))
    if arms:manifest['live_arms']=sorted(live)
    if seed_dirs:manifest['seed_dirs']={a:str(Path(p).resolve()) for a,p in seed_dirs.items() if Path(p).is_dir()}
    if policy_overrides:manifest['policy_overrides']=dict(policy_overrides)
    validate(manifest)
    return manifest


def validate(manifest):
    if (manifest.get('schema') != 'v3-open-production-v1' or manifest.get('action') != 'open-production-cohort'
            or manifest.get('mode') != 'production' or manifest.get('profile') not in PROFILES
            or sorted(manifest.get('arms',[])) != sorted(ARMS) or not set(manifest.get('live_arms',ARMS)) <= set(ARMS)
            or manifest.get('cap_microusd') != CAP_MICROUSD
            or manifest.get('infrastructure_contingency_microusd') != 0
            or not re.fullmatch('[A-Za-z0-9_-]{1,80}',manifest.get('cohort_id',''))):
        raise DeploymentRefused('fixed two-arm $80 open-production configuration required')
    if any(manifest.get(k) for k in ('fixtures','synthetic','fake_provider','pacing','accelerated_clock','test_clock')):
        raise DeploymentRefused('swarm check cannot use offline fixtures or clocks')
    if 'policy_overrides' in manifest and not isinstance(manifest['policy_overrides'],dict):
        raise DeploymentRefused('policy_overrides must be a JSON object')
    validate_problem(manifest['problem'])
    if manifest['problem_id'] != 'b3-'+Path(manifest['problem']['source']).stem:
        raise DeploymentRefused('open-production problem identity mismatch')
    _, pin = production_release(manifest['production_release'])
    for key in ('accepted_root','candidate_commit','release_root','release_sha256','release_files','runtime_pin'):
        if manifest[key] != pin[key]:raise DeploymentRefused('release identity changed: '+key)
    profile = manifest['profile']
    policy = load_contracts(Path(pin['release_root'])/'contracts',expected_sha256=manifest['contracts_sha256'])
    require_current_profile(policy, profile)
    if manifest['arm_profiles'] != _json_value(policy[profile]) or manifest['route_layouts'] != route_layouts(policy, profile):
        raise DeploymentRefused('open-production profiles or capacity changed')
    if manifest['clocks'] != PROFILES[profile]['clocks']:raise DeploymentRefused('open-production clocks changed')
    if manifest.get('live_qualification') != QUALIFICATION:
        raise DeploymentRefused('route qualification protocol changed')
    target = manifest['target']
    if (not target['nodes'] or not (target['nodes'] == ['*'] or set(target['nodes']) <= set(pin['target_nodes']))
            or target['partition'] != pin['target_partition'] or {k:target[k] for k in ('cpus','memory_gib')} != __import__('v2.check_sizing',fromlist=['x']).size(manifest.get('live_arms',list(ARMS)))
            or target['seconds'] != PROFILES[profile]['seconds']):
        raise DeploymentRefused('open-production compute target changed')
    if manifest['local_root'] != '/tmp/v3-'+manifest['profile']+'-'+manifest['cohort_id']:
        raise DeploymentRefused('open-production scratch identity changed')
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
    parser.add_argument('--profile',default=PROFILE,choices=sorted(PROFILES))
    parser.add_argument('--seed',action='append',default=[],help='arm=dir continuation seed')
    parser.add_argument('--problem',type=int,default=PROBLEM,help='batch-3 problem number (1-10)')
    parser.add_argument('--policy-overrides',default=None,help='JSON object deep-merged over each swarm policy.json for this run')
    args = parser.parse_args(argv)
    output = Path(args.output).absolute()
    manifest = prepare(args.release,cohort=args.cohort,durable_root=args.durable_root,
        credential_file=args.credential_file,nodes=args.node,arms=[a for a in args.arms.split(',') if a] or None,
        profile=args.profile,seed_dirs=dict(x.split('=',1) for x in args.seed) or None,policy_overrides=_overrides(args.policy_overrides),problem=args.problem)
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
