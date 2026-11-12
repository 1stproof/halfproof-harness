"""Explicit offline SLURM-chain fixture; never reads an inference credential."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from . import chain

SCHEMA='hc-offline-chain-fixture-v1'


def close(run,directory):
    chain.save(Path(directory)/'CLAIMS-MANIFEST.json',dict(claims=[dict(current_record=1,certified=True,text='Offline evidence',statement='Fixture',premise_refs=[])]))


def writing(run,manifest,directory):
    from writing import write,control
    class MockClient:
        def __init__(self,*args,**kwargs):pass
        def own_call(self,system,payload,name,*args,**kwargs):
            if name=='plan' or name=='fresh-roster-plan':return json.dumps(dict(main_records=[1],included_records=[1],dependencies={'1':[]},source_dependency_dispositions=[]))
            if 'referee' in name:return json.dumps(dict(accept=True,statements=[dict(label='fixture',proof_complete=True,source_certified=True,needed_for_main=True,dependencies_complete=True)]))
            if 'selection' in name:return json.dumps(dict(candidate=0,reason='Mock roster choice'))
            return r'\documentclass{article}\begin{document}Offline mock manuscript.\end{document}'
    def compile_tex(source,directory):
        chain.write(Path(directory)/'FINAL.tex',source.encode());chain.write(Path(directory)/'FINAL.pdf',b'%PDF-mock')
        return dict(ok=True,pages=1)
    def execute(command,**kwargs):
        import runpy
        previous=sys.argv
        try:
            sys.argv=command[1:]
            with patch('writing.runtime.Client',MockClient),patch('writing.runtime.compile_tex',compile_tex),patch.object(write,'Client',MockClient),patch.object(write,'compile_tex',compile_tex),patch.object(control,'execute',side_effect=lambda args,deadline:write.run(args)),patch('urllib.request.urlopen',side_effect=AssertionError('network prohibited in offline fixture')):
                runpy.run_path(command[1],run_name='__main__')
        finally:sys.argv=previous
        return subprocess.CompletedProcess(command,0)
    out=chain.writer(run,manifest,directory,execute=execute)
    # Both mount names must expose the exact coordinator bytes.
    statement=Path(run['problem']['source']).read_bytes()
    for attempt in out.glob('attempt-*'):
        if (attempt/'ORIGINAL-PROBLEM.tex').is_file():
            assert (attempt/'ORIGINAL-PROBLEM.tex').read_bytes()==statement
    return out


def advance(registry):
    state=chain.load(registry)
    if state['run'].get('schema')!=SCHEMA or state['run'].get('fake_provider') is not True:
        raise ValueError('offline fixture identity required')
    destination=chain.advance(registry,closing=close,writing=writing)
    assert (destination/'paper.tex').stat().st_size and (destination/'paper.pdf').stat().st_size
    return destination


def main():
    from v2.deployment import require_compute,problem_mounts
    from v2.production import supplied_problem
    from v2.contracts_v3 import contracts_sha256,load_contracts
    require_compute()
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--statement-file',required=True);p.add_argument('--seed');a=p.parse_args()
    root=Path(a.root);root.mkdir(parents=True,exist_ok=False)
    # Hash the immutable archive extracted by the initial sbatch; successors use bin/v3-job.
    source=Path(__file__).resolve().parents[1];files=chain.inventory(source)
    h=hashlib.sha256()
    for n,v in sorted(files.items()):h.update(n.encode()+b'\0'+v.encode()+b'\n')
    problem=supplied_problem(4,a.statement_file)
    mounts=problem_mounts(type('Config',(),dict(problem_mount=problem,deployment_mode='production'))())
    assert mounts[1]==mounts[4]==problem['source']
    from . import production
    release_pin=dict(accepted_root=str(root/'offline-accepted'),candidate_commit='offline-fixture',release_root=str(source),
        release_files=files,release_sha256=h.hexdigest(),runtime_pin={},target_nodes=['*'],target_partition='normal')
    # The release acceptance service is mocked. Production argument parsing,
    # statement/seed validation, profile sizing and sbatch argv are real.
    launcher_output=root/'production-launch.json'
    argv=['--release',str(root/'offline-release.json'),'--problem','4','--statement-file',a.statement_file,
        '--cohort','siebert-mock-'+os.environ['SLURM_JOB_ID'],'--durable-root',str(root/'run'),
        '--credential-file','/nonexistent-offline-credential','--node','*','--arms','mixed-swarm',
        '--output',str(launcher_output)]
    if a.seed:argv+=['--seed','mixed-swarm='+a.seed]
    with patch.object(production,'production_release',return_value=(root/'offline-release.json',release_pin)):
        production.main(argv)
    run=chain.load(launcher_output)
    run.update(schema=SCHEMA,action='chain',fake_provider=True)
    registry=chain.register(run)
    chain.save(root/'submission.json',dict(registry=str(registry),first_job=os.environ['SLURM_JOB_ID'],successors=chain.load(registry)['successors'],statement=problem,seed_dirs=run.get('seed_dirs',{})))
    print('Initial job registered afterany successor and exits without advancing stages',flush=True)

if __name__=='__main__':main()
