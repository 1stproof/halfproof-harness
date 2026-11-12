"""Finite, deterministic, non-blinded batch schedule and disk oracle."""
import json
from .control import atomic_json
FIELDS=('last_completed_batch','unfinished_batch','rejected_counterexample','invariant_version','changed_assumption','next_action')

def expected(index):
    return dict(zip(FIELDS,[index,None,'AB != BA at (1,0) mod 5','gcd-v1',
        'include modulus 8' if index>=4 else 'prime moduli only','read next batch']))

def reveal(work, elapsed, interval=1800, count=9):
    directory=work/'batches'; directory.mkdir(exist_ok=True)
    for index in range(min(count,int(elapsed//interval)+1)):
        target=directory/f'{index:03}.md'
        if not target.exists():
            target.write_text(f'Batch {index}: audit A, B, AB, BA on (1,0) modulo 5,7,11.'+
                (' Include modulus 8 from now on.' if index>=4 else '')+'\n')
    if elapsed >= count*interval: (directory/'END').touch()

def oracle(work, index):
    try: data=json.loads((work/'NOTES.md').read_text())
    except (OSError,ValueError): return dict.fromkeys(FIELDS,False)
    if not isinstance(data,dict):return dict.fromkeys(FIELDS,False)
    return {key:data.get(key)==value for key,value in expected(index).items()}


def stress_page(index):
    """Bounded synthetic data, <1 CPU second for the whole corpus.
    Token counts are measured from provider usage, never inferred from bytes.
    """
    rows=[]
    for i in range(2048):
        a=(i*37+index*101)%997; b=(i*71+index*43)%991
        rows.append(f'{index}:{i} {a} {b} {(a+b)%997}')
    return '\n'.join(rows)+'\n'

def stress_schedule(work, state, lane, session, spent_tokens, compacted):
    if lane not in ('dsf','qwen24t','glm'): return
    eligible=(session==1 or (lane in ('dsf','qwen24t') and session>=3))
    if not eligible:return
    corpus=state.setdefault('stress',{'pages':0,'sessions':{},'total_token_envelope':24*98304})
    key=str(session)
    item=corpus['sessions'].setdefault(key,{'pages':0,'closed':False})
    if item['closed']:return
    directory=work/'traces';directory.mkdir(exist_ok=True)
    limit=24 if lane=='glm' else 12
    if compacted or spent_tokens>=limit*98304 or corpus['pages']>=24:
        item['closed']=True
        (directory/'STOP').write_text('Stop loading trace pages. Recover the arithmetic audit from NOTES.md.\n')
        return
    if item['pages']<limit:
        # One bounded page per schedule step after a model request; no user-message injection.
        index=corpus['pages'];(directory/f'{index:03}.txt').write_text(stress_page(index))
        corpus['pages']+=1;item['pages']+=1


def revision_oracle(work, index):
    try:data=json.loads((work/'NOTES.md').read_text())
    except (OSError,ValueError):return False
    return isinstance(data,dict) and data.get('checkpoint_revision')==index and 'superseded_revision' in data and data['superseded_revision']==(index-1 if index else None)


def routed_fake(function):
    """Legacy offline providers advertise their mock endpoint before their frames.

    Only used by test fixtures. Actual HTTP transports never call this adapter;
    explicit off-route metadata in a fixture is preserved and still rejected.
    """
    import functools
    @functools.wraps(function)
    def call(self, request, *args, **kwargs):
        result=function(self, request, *args, **kwargs)
        from contracts.route import routes
        if request.get('model') not in dict(routes().values()):return result
        class Frames:
            def __init__(self):self.first=True;self.iterator=iter(result)
            def __iter__(self):return self
            def __next__(self):
                if self.first:
                    self.first=False
                    return ('data: '+json.dumps(dict(model=request['model'],
                        provider=request['provider']['only'][0],choices=[dict(index=0,delta={},finish_reason=None)]))+'\n\n').encode()
                return next(self.iterator)
            def __getattr__(self,name):return getattr(result,name)
        return Frames()
    return call
