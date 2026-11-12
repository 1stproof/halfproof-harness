"""Production runtime: no synthetic faults, traces, or fixture deadlines."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from .campaign import load_config, BASE
from .control import atomic_json
from .gateway import Gateway
from .lifecycle import Watchdog
from .policy import error_class
from .release import pin_release
from .runtime import MODELS
from .worker import QwenWorker
from .owned import run_owned

class CampaignWatchdog(Watchdog):
    def tick(self,*args,**kwargs):
        before=dict(self.state.get('snapshots',{}))
        result=super().tick(*args,**kwargs)
        after=self.state.get('snapshots',{})
        names={'NOTES.md','EXPERIMENTS.md','FINAL.tex', 'PROVENANCE.md','AUDIT.md'} if self.run.writeup else {'NOTES.md','EXPERIMENTS.md'}
        if any(name in after and before.get(name)!=after[name] for name in names):
            self.state['campaign_last_qualifying_write']=self.clock();self.save()
        return result

    def provider_error(self,status):
        counts=self.state.setdefault('campaign_provider_errors',{})
        kind=error_class(status);counts[kind]=counts.get(kind,0)+1
        super().provider_error(status)
        self.save()

def reopen(w, start, wall, cap):
    """Never reallocate money or time. Only scheduler interruptions may reopen."""
    if w.state.get('scientific_frozen') or w.state.get('closure_manifest'):
        raise ValueError('scientifically closed arm cannot resume')
    if (w.run.start,w.run.wall,w.run.cap,w.ledger.cap)!=(start,wall,cap,cap):
        raise ValueError('original allocation mismatch')
    terminal=w.state.get('terminal')
    if (terminal and terminal['reason']=='slurm-interrupted') or w.state.get('closing')=='slurm-interrupted' or w.state.get('campaign_interrupted'):
        if terminal:w.state.setdefault('campaign_interruptions',[]).append(terminal)
        w.state['terminal']=None;w.state.pop('closing',None);w.state.pop('campaign_interrupted',None)
        w.run.phase='write-up' if w.run.writeup else 'research'
        w.save()

def interrupt(w):
    # Close gateway admission first. Checkpoint is a durable harness snapshot;
    # no last-minute paid request is admitted to solicit a model checkpoint.
    with w.mutex:
        w.state['campaign_interrupted']=True
        w.state['campaign_resume_phase']=w.run.phase
        w.run.phase='paused-alert';w.save()
        atomic_json(w.path/'campaign-checkpoint.json',dict(start=w.run.start,
                    deadline=w.run.start+w.run.wall,ledger=w.ledger.snapshot(),session=w.state['session']))
        if w.closure is not None:
            w.stop_worker();w.save()
        else:w.finish('slurm-interrupted')

def preflight(w,release,lane):
    """Compile a constant TeX document using the worker's actual sandbox mounts."""
    from .sandbox import command
    run=w.path/'preflight';run.mkdir(exist_ok=True)
    control=run/'control';control.mkdir(exist_ok=True)
    state=run/'state';state.mkdir(exist_ok=True)
    work=run/'work';work.mkdir(exist_ok=True)
    from .tex import FIXED, install_template, build
    install_template(w.work)
    (work/'probe.tex').write_text(FIXED)
    cfg=SimpleNamespace(release=release,lane=lane,model=MODELS[lane][0],run=run,
                        control=control,state=state,work=work,seconds=60,scenario='normal',research_live=True)
    try:
        cmd,env=command(cfg)
        # Preserve the exact mounts and environment, replace only the payload.
        index=cmd.index('/bin/bash')
        cmd=cmd[:index]+['/bin/sh','-c','pdflatex -interaction=batchmode -halt-on-error -no-shell-escape probe.tex >/dev/null 2>&1 && test -s probe.pdf']
        result=run_owned(cmd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90)
        passed=result.returncode==0 and build(w.work,FIXED.encode())[0]['outcome']=='pass'
    except (OSError,ValueError,subprocess.SubprocessError):passed=False
    w.state['campaign_build']='passed' if passed else 'failed';w.save()
    return passed

def swarm_parent(row, **services):
    """One parent, dynamic model/role contexts; never a per-replica model closure."""
    from .swarm_runtime import SwarmRuntime
    strategy=row.get('strategy',row.get('lane'))
    if strategy not in ('dsf-swarm','mixed-swarm','open-swarm') or row.get('replica',1)!=1:
        raise ValueError('one declared swarm parent required')
    return SwarmRuntime(strategy=strategy,**services)

def run(path,name):
    raise ValueError('legacy independent worker retired; use the v3 cohort service factory')

if __name__=='__main__':
    try:raise SystemExit(run(Path(sys.argv[1]),sys.argv[2]))
    except Exception:
        print('campaign worker failed; retained original allocation for resume',file=sys.stderr)
        raise SystemExit(2)
