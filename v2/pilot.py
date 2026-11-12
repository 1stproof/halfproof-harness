"""Section 11 six-arm pilot preparation/submission; never automatic launch.

Default invocation writes a reviewable manifest and prints the Slurm command.
Only --submit can submit. Credentials are never opened by this launcher.
"""
import os
import argparse
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .contracts_v3 import load_contracts, contracts_sha256
from .deployment import DeploymentRefused, slurm_command
from .control import atomic_json

PROBLEM=Path(os.environ.get('HARNESS_PROBLEMS', str(Path(__file__).resolve().parents[1]/'examples/batch-3/problems')))/'p02.tex'
PROBLEM_SHA256='0a28765947f9e5ff3533af2487722ccb05addaa3b1e229843b098b15b09db66f'  # public Batch 3 statement (author e-mail lines removed)
ARMS=('dsf-solo','dsp-solo','qwen24t-solo','k3-solo','dsf-swarm','mixed-swarm')
QUALIFICATION=dict(ramp_stop_seconds=5400,
    rungs={'dsf':[1,2,4,8,12],'dsp':[1,2],'qwen24t':[1,2],'k3':[1,2,3],'glm':[1]},
    waves=2,overlap_seconds=30,maximum_repeat_incidents=1)


def _json_value(value):
    """Copy frozen contract mappings/sequences into JSON wire containers."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def problem_record(path=PROBLEM):
    path=Path(path)
    if path!=PROBLEM or path.is_symlink() or not path.resolve(strict=True).is_file():
        raise DeploymentRefused('pilot requires the operator-declared regular b3-problems/p02.tex')
    before=path.stat();raw=path.read_bytes();after=path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise DeploymentRefused('pilot problem changed during pin')
    if not raw or len(raw)>16*1024*1024:raise DeploymentRefused('pilot problem size invalid')
    if hashlib.sha256(raw).hexdigest()!=PROBLEM_SHA256:
        raise DeploymentRefused('operator problem differs from accepted input pin')
    return dict(source=str(path),sha256=hashlib.sha256(raw).hexdigest(),target='/work/PROBLEM.tex')


def accepted_release(root):
    root=Path(root).resolve(strict=True)
    if (root/'local-release.json').is_file():
        # Package built on this machine by bin/package-release: no external signature.
        local=json.loads((root/'local-release.json').read_bytes())
        if local.get('schema')!='local-release-v1':
            raise DeploymentRefused('unknown local release schema')
        release=dict(candidate_commit=local['commit'],qualified_nodes=local['target_nodes'])
        final=dict(runtime_pin=local['runtime_pin'],
                   limits=dict(target_nodes=local['target_nodes'],target_partition=local['target_partition']))
        return root,release,final
    release=json.loads((root/'release.json').read_bytes())
    signature=json.loads((root/'p8-signature.json').read_bytes())
    final=json.loads((root/'manifest.json').read_bytes())
    if (signature.get('status')!='ACCEPTED' or signature.get('signer_package')!='P8'
            or set(signature['scope'])!=set(ARMS)
            or any(signature['scope'][arm]!='PASS' for arm in ARMS)):
        raise DeploymentRefused('pilot requires independent acceptance of all six arms')
    for key in ('candidate_commit','release_content_sha256'):
        if signature[key]!=release[key] or release[key]!=final[key]:
            raise DeploymentRefused('accepted release identities differ')
    if hashlib.sha256((root/'p8-signature.json').read_bytes()).hexdigest()!=release['p8_signature_sha256']:
        raise DeploymentRefused('accepted signature changed')
    if hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()!=signature['test_manifest_sha256']:
        raise DeploymentRefused('accepted test manifest changed')
    return root,release,final


def route_layouts(policy):
    if all(policy['pilot'][arm].get('orchestration')=='stateless-v1' for arm in ('dsf-swarm','mixed-swarm')):
        ceilings={'dsf':12,'dsp':2,'qwen24t':2,'k3':3,'glm':1}
        result={}
        for model,capacity in ceilings.items():
            solo=model+'-solo';solos=[solo] if solo in policy['pilot'] else []
            limits={arm:1 for arm in solos}
            if model=='dsf':limits['dsf-swarm']=capacity-len(solos)
            limits['mixed-swarm']=capacity-len(solos)
            result[model]=dict(capacity=capacity,arm_limits=limits,reserved_solos=solos)
        return result
    # These are upper experimental rungs, not claims of measured live capacity.
    # Per-request admission remains subject to the ramp's current lower rung.
    return {key:dict(capacity=sum(row['slots'].get(key,0) for row in policy['pilot'].values()),
        arm_limits={arm:row['slots'][key] for arm,row in policy['pilot'].items() if key in row['slots']},
        reserved_solos=[arm for arm,row in policy['pilot'].items() if arm.endswith('-solo') and key in row['slots']])
        for key in policy['models']}


def require_current_profile(policy):
    """Do not launch the superseded fixed-pool swarm under a new-design label."""
    for arm,model,cap,reserve in (('dsf-swarm','dsf',60000000,12000000),
                                ('mixed-swarm','k3',50000000,16000000)):
        row=policy['pilot'][arm]
        closing=row.get('closing_reserves_microusd',{})
        if (row.get('orchestration')!='stateless-v1' or row.get('orchestrator')!=model
                or row.get('final_author')!=model or row['cap_microusd']!=cap
                or sum(closing.values())!=reserve or row.get('revision_rounds')!=1
                or row.get('research_stop_seconds')!=7200 or row['wall_seconds']!=10800
                or any(key in row for key in ('breadth_stop_seconds','model_pools_microusd','review_ticket_limit'))):
            raise DeploymentRefused('pilot requires current stateless orchestration and protected closing contract')


def prepare(accepted, *, cohort, durable_root, credential_file, nodes, partition='normal'):
    if not re.fullmatch('[A-Za-z0-9_-]{1,80}',cohort):raise DeploymentRefused('invalid cohort identity')
    root,release,final=accepted_release(accepted)
    policy=load_contracts(root/'source/contracts')
    require_current_profile(policy)
    if set(policy['pilot'])!=set(ARMS) or policy['pilot_total_microusd']!=180000000:
        raise DeploymentRefused('fixed six-arm $180 pilot profile required')
    if (not nodes or not set(nodes)<=set(release.get('qualified_nodes',[]))
            or not set(nodes)<=set(final['limits']['target_nodes']) or partition!=final['limits']['target_partition']):
        raise DeploymentRefused('pilot target must be independently qualified')
    durable=Path(durable_root).absolute()
    if any(p in ('runs-v2','web-harness-v2') for p in durable.parts):
        raise DeploymentRefused('legacy paths prohibited')
    if durable.exists():raise DeploymentRefused('new unique pilot durable root required')
    manifest=dict(schema='v3-paid-pilot-v1',action='pilot-cohort',mode='pilot',profile='pilot',
        accepted_root=str(root),candidate_commit=release['candidate_commit'],
        release_root=str(root/'source'),release_sha256=release['release_content_sha256'],
        release_files=final['release_files'],runtime_pin=final['runtime_pin'],contracts_sha256=contracts_sha256(root/'source/contracts'),
        cohort_id=cohort,problem_id='b3-p02',problem=problem_record(),arms=list(ARMS),
        local_root='/tmp/v3-pilot-'+cohort,durable_root=str(durable),
        credential_file=str(Path(credential_file).absolute()),
        fixtures=[],synthetic=False,fake_provider=False,
        cap_microusd=180000000,infrastructure_contingency_microusd=10000000,
        arm_profiles=_json_value(policy['pilot']),route_layouts=route_layouts(policy),
        clocks=dict(research_stop_seconds=7200,authoring_stop_seconds=10800,cleanup_stop_seconds=11220),
        live_qualification=json.loads(json.dumps(QUALIFICATION)),
        withdrawal_authority='operator',
        target=dict(partition=partition,nodes=nodes,cpus=16,memory_gib=64,seconds=14400))
    validate(manifest)
    return manifest


def validate(manifest):
    if not re.fullmatch('[A-Za-z0-9_-]{1,80}',manifest.get('cohort_id','')):
        raise DeploymentRefused('invalid pilot cohort identity')
    if (manifest.get('schema')!='v3-paid-pilot-v1' or manifest.get('action')!='pilot-cohort'
            or manifest.get('mode')!='pilot' or manifest.get('profile')!='pilot'
            or set(manifest.get('arms',[]))!=set(ARMS) or len(manifest['arms'])!=6
            or manifest.get('cap_microusd')!=180000000
            or manifest.get('infrastructure_contingency_microusd')!=10000000):
        raise DeploymentRefused('fixed six-arm pilot configuration required')
    if any(manifest.get(k) for k in ('fixtures','synthetic','fake_provider','pacing','accelerated_clock','test_clock')):
        raise DeploymentRefused('paid pilot cannot use offline synthetic fixtures or clocks')
    if manifest['problem']!=problem_record():
        raise DeploymentRefused('pilot real-problem pin changed')
    policy=load_contracts(expected_sha256=manifest['contracts_sha256'])
    require_current_profile(policy)
    if manifest['arm_profiles']!=_json_value(policy['pilot']) or manifest['route_layouts']!=route_layouts(policy):
        raise DeploymentRefused('pilot caps, pools, roles or route limits changed')
    if manifest['clocks']!=dict(research_stop_seconds=7200,authoring_stop_seconds=10800,cleanup_stop_seconds=11220):
        raise DeploymentRefused('original pilot clock configuration changed')
    if manifest.get('live_qualification')!=QUALIFICATION:
        raise DeploymentRefused('route qualification protocol changed')
    root,release,final=accepted_release(manifest['accepted_root'])
    if manifest.get('runtime_pin')!=final['runtime_pin']:
        raise DeploymentRefused('pilot dependency pin differs from accepted release')
    target=manifest['target']
    if (not target['nodes'] or not set(target['nodes'])<=set(release.get('qualified_nodes',[]))
            or target['partition']!=final['limits']['target_partition']
            or target['cpus']!=16 or target['memory_gib']!=64 or target['seconds']!=14400):
        raise DeploymentRefused('pilot target differs from accepted cold qualification')
    if (manifest['release_root']!=str(root/'source') or manifest['release_files']!=final['release_files']
            or manifest['candidate_commit']!=release['candidate_commit']
            or manifest['release_sha256']!=release['release_content_sha256']):
        raise DeploymentRefused('pilot source differs from independently accepted release')
    return True


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-release',required=True)
    parser.add_argument('--cohort',required=True)
    parser.add_argument('--durable-root',required=True)
    parser.add_argument('--credential-file',required=True,help='path only; never read by launcher')
    parser.add_argument('--node',action='append',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--submit',action='store_true')
    args=parser.parse_args(argv)
    manifest=prepare(args.accepted_release,cohort=args.cohort,durable_root=args.durable_root,
        credential_file=args.credential_file,nodes=args.node)
    output=Path(args.output).absolute()
    if output.exists():raise DeploymentRefused('pilot manifest already exists; never replace original launch intent')
    atomic_json(output,manifest)
    command=slurm_command(output,manifest,output=str(output)+'.slurm.log')
    print(json.dumps(dict(manifest=str(output),argv=command,submitted=False,
        cap_microusd=180000000,arms=list(ARMS),problem=str(PROBLEM))),flush=True)
    if args.submit:
        # Exclusive launch journal prevents a retry from buying a new cohort.
        journal=Path(str(output)+'.submission.json')
        with journal.open('x') as stream:
            stream.write(json.dumps(dict(status='submission-attempted',argv=command))+'\n')
        result=subprocess.run(command,check=True,capture_output=True,text=True)
        atomic_json(journal,dict(status='submitted',job_id=result.stdout.strip(),argv=command))
        print(result.stdout.strip(),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
