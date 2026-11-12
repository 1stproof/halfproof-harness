"""Durable-note consolidation; archives are harness writes, never progress."""
import hashlib
import json
import os
import stat
import uuid
from functools import lru_cache
from pathlib import Path
from .artifacts import report_content

PAIR=('NOTES.md','EXPERIMENTS.md')

@lru_cache(maxsize=5)
def trigger(lane):
    from .runtime import MODELS
    settings=json.loads((Path(__file__).resolve().parents[1]/'bin'/f'web-system-settings.{lane}.json').read_text())
    provider=next(x for x in settings['modelProviders']['openai'] if x['id']==MODELS[lane][0])
    return provider['generationConfig']['contextWindowSize']*settings['context']['autoCompactThreshold']

def pair_size(work):
    total=0
    for name in PAIR:
        try:
            s=(work/name).lstat()
            if not stat.S_ISREG(s.st_mode):return None
            total+=s.st_size
        except FileNotFoundError:pass
    return total

def archive(work):
    # Root is created before the worker starts and mounted read-only in Qwen.
    root=work/'notes-archive'
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    stage='.archive-'+uuid.uuid4().hex
    try:
        os.mkdir(stage,dir_fd=fd)
        dest=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            hashes={}
            for name in PAIR:
                try:size=(work/name).lstat().st_size
                except FileNotFoundError:size=0
                data=report_content(work,name,limit=size)
                if len(data)!=size:raise OSError('unstable checkpoint; retry at next boundary')
                out=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444,dir_fd=dest)
                with os.fdopen(out,'wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                hashes[name]=hashlib.sha256(data).hexdigest()
            os.fsync(dest)
        finally:os.close(dest)
        # Only a complete durable pair is published as a numbered archive.
        n=max((int(x) for x in os.listdir(fd) if x.isdecimal()),default=0)+1
        os.rename(stage,str(n),src_dir_fd=fd,dst_dir_fd=fd)
        os.fsync(fd)
        return {'archive':f'notes-archive/{n}','hashes':hashes}
    finally:
        try:
            for name in PAIR:
                try:os.unlink(stage+'/'+name,dir_fd=fd)
                except FileNotFoundError:pass
            os.rmdir(stage,dir_fd=fd)
        except FileNotFoundError:pass
        os.close(fd)


def release_notice(w):
    w.state['notice']=None
    if w.worker is not None:w.worker.notice(None)
    if w.run.writeup and not w.run.notice_delivered:w.run.notice_due=w.clock()

def watch(w,boundary):
    """Return whether consolidation currently precedes ordinary notices/write-up."""
    size=pair_size(w.work)
    if size is None:return False
    threshold=trigger(w.run.lane)*4
    c=w.state.get('consolidation')
    if c and c['status'] in ('queued','notified','repeat') and size < .2*threshold:
        # Only changes to the live pair can complete the cycle.
        c.update(status='done',completed_at=w.clock(),final_bytes=size,
                 within_15_minutes=w.clock()-c.get('sent_at',w.clock())<=900)
        w.event('consolidation-complete')
        w.state['consolidation_history'].append(dict(c))
        release_notice(w)
    if c and c['status'] in ('queued','notified','repeat') and w.run.writeup and not c['writeup_priority']:
        if size<=.8*threshold:
            c.update(status='skipped',finished_at=w.clock())
            w.state['consolidation_history'].append(dict(c));release_notice(w)
            return False
        c['writeup_priority']=True
    if c and c['status'] in ('done','abandoned','skipped'):
        if size < .2*threshold:c['rearmed']=True
        if not c.get('rearmed') or size <= (.8 if w.run.writeup else .4)*threshold:return False
        c=None
    if c is None:
        if size <= (.8 if w.run.writeup else .4)*threshold:return False
        if not boundary:return True
        try:copied=archive(w.work)
        except OSError:return False
        c=dict(copied,status='queued',writeup_priority=w.run.writeup,initial_bytes=size,trigger_tokens=threshold/4)
        w.state['consolidation']=c
        w.state.setdefault('consolidation_history',[])
        w.event('consolidation-archived');w.save()
    if c['status']=='queued' and boundary:
        c.update(status='notified',sent_at=w.clock())
        w.notice('consolidate')
    elif c['status']=='notified' and w.clock()-c['sent_at']>=900 and boundary:
        c.update(status='repeat',repeat_at=w.clock())
        w.notice('consolidate-repeat')
    elif c['status']=='repeat':
        notice=w.state['notice']
        if (notice and notice['kind']=='consolidate-repeat' and notice['attached']) or w.clock()-c['repeat_at']>=240:
            c.update(status='abandoned',finished_at=w.clock())
            w.state['consolidation_history'].append(dict(c))
            w.event('consolidation-carry-on');release_notice(w)
            return False
    return c['status'] in ('queued','notified','repeat')
