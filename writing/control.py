"""Precommitted writing attempts. Evaluation is a separate operator command."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from .core import save, digest
from .runtime import require_slurm
from .safety import policy, pinned_route, deliver, choose, statement_diagnostics

B=Path(os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data')))/'writing'


def submit(script,name,output,seconds=21600,cpus=2,memory='8G'):
    return int(subprocess.check_output(['sbatch','--parsable','--partition=normal',
        '--cpus-per-task='+str(cpus),'--mem='+memory,'--chdir='+str(B),
        '--time='+str(seconds//60),'--job-name='+name,'--output='+str(output),str(script)],
        text=True).strip().split(';')[0])


def job_script(archive,module,args):
    return ('#!/bin/bash\nset -euo pipefail\nTASK_SOURCE=$(mktemp -d /tmp/writing-source.XXXXXX)\n'
        +'tar -xf '+shlex.quote(str(archive))+' -C "$TASK_SOURCE"\ncd "$TASK_SOURCE"\n'
        +'export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nexec python3 -m '+module+' '
        +' '.join(shlex.quote(str(x)) for x in args)+'\n')


def execute(args,deadline):
    argv=[sys.executable,'-m','writing.write','--single-attempt']
    for key,value in vars(args).items():
        if value is not None and key!='single_attempt':
            argv.extend(['--'+key.replace('_','-'),str(value)])
    child=subprocess.Popen(argv,start_new_session=True)
    try: child.wait(timeout=max(0,deadline-time.time()) if deadline is not None else None)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid,signal.SIGTERM)
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGKILL);child.wait()
    return child.returncode


def recover(out):
    out=Path(out);candidates=[];rejected=set();flags=[]
    if (out/'candidate-index.json').exists():
        for row in json.loads((out/'candidate-index.json').read_text()):
            path=Path(row['directory'])/'FINAL.tex'
            if path.exists():
                raw=path.read_bytes()
                if digest(raw)!=row['sha256']:
                    rejected.add(path);flags.append('candidate_hash_mismatch:'+str(path))
                    continue
                row['manuscript']=raw.decode('utf-8')
                compiled=path.parent/'compilation.json'
                if compiled.exists():row['compilation']=json.loads(compiled.read_text())
                candidates.append(row)
    plan=json.loads((out/'plan.json').read_text()) if (out/'plan.json').exists() else {}
    selection=json.loads((out/'selection.json').read_text()) if (out/'selection.json').exists() else {}
    indexed={Path(c['directory'])/'FINAL.tex' for c in candidates}
    for path in sorted(out.glob('candidate-*/FINAL.tex')):
        if path in indexed or path in rejected:continue
        source=path.read_bytes().decode('utf-8')
        from .runtime import clean_tex
        try:clean_tex(source)
        except ValueError:continue
        compiled=path.parent/'compilation.json'
        comp=json.loads(compiled.read_text()) if compiled.exists() else dict(ok=False,errors=['compile not checkpointed'])
        candidates.append(dict(candidate=len(candidates),directory=str(path.parent),manuscript=source,
            sha256=digest(source.encode()),compilation=comp,review={},
            violations=statement_diagnostics(source,plan.get('included_records',[]))))
    if flags:save(out/'recovery-flags.json',flags)
    return candidates,plan,selection.get('candidate')


def publish_checkpoint(root, flags=()):
    root=Path(root);selected=[];plans={};all_flags=list(flags)
    config=json.loads((root/'precommit.json').read_text())
    if (root/'attempts.json').exists():
        all_flags.extend(row['failure'] for row in json.loads((root/'attempts.json').read_text()) if row.get('failure'))
    for out in sorted(root.glob('attempt-*')):
        candidates,plan,editor=recover(out)
        flagfile=out/'recovery-flags.json'
        if flagfile.exists():all_flags.extend(json.loads(flagfile.read_text()))
        c=choose(candidates,editor)
        if c:
            c=dict(c,candidate=len(selected));plans[c['candidate']]=plan;selected.append(c)
    best=choose(selected)
    return deliver(root,selected,plans.get(best['candidate'],{}) if best else {},
        best['candidate'] if best else None,flags=all_flags,
        contract_hash=digest(Path(config['arguments']['contract']).read_bytes()))


def schedule(args,runner=None):
    require_slurm()
    runner=runner or execute
    contract=json.loads(Path(args.contract).read_text())
    pinned_route(contract);rules=policy(contract)
    root=Path(args.out);root.mkdir(parents=True,exist_ok=True)
    config=dict(arguments=vars(args),policy=rules,contract=contract)
    pin=root/'precommit.json'
    if pin.exists() and json.loads(pin.read_text())!=config:raise ValueError('precommitted configuration changed')
    save(pin,config)  # durable before the first request
    selected=[];plans={};attempts=[];all_flags=[]
    publish_checkpoint(root,flags=['writing_in_progress'])
    for number in range(1,rules['attempts']+1):
        out=root/('attempt-'+str(number))
        failure=None;returncode=None;signum=None
        if not out.exists() and (rules['deadline'] is None or time.time()<rules['deadline']):
            own=argparse.Namespace(**vars(args));own.out=str(out);own.attempt=number
            try:
                returncode=runner(own,rules['deadline'])
                if returncode:
                    signum=-returncode if returncode<0 else None
                    failure='child_exit:'+str(returncode)+((':signal:'+str(signum)) if signum else '')
            except (OSError,RuntimeError) as exc:failure=type(exc).__name__+': '+str(exc)
        candidates,plan,editor=recover(out)
        # The own editor chooses within an attempt; the fixed fallback covers failure.
        c=choose(candidates,editor)
        if c:
            c=dict(c,candidate=len(selected));plans[c['candidate']]=plan;selected.append(c)
        attempts.append(dict(attempt=number,started=out.exists(),complete_candidates=len(candidates),failure=failure,returncode=returncode,signal=signum))
        save(root/'attempts.json',attempts)
        if failure:all_flags.append(failure)
        if time.time()>=rules['deadline']:all_flags.append('deadline')
        publish_checkpoint(root,all_flags)
    return json.loads((root/'delivery/manifest.json').read_text())


def main(a):
    # Config contains the complete release-write argument set. No experiment state.
    schedule(argparse.Namespace(**json.loads(Path(a.config).read_text())))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);main(parser.parse_args())
