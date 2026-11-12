"""Synthetic, non-blinded large-state fixture; never reads another run."""
import json
from pathlib import Path

def seed_large_state(work):
    sizes={'NOTES.md':240*1024,'EXPERIMENTS.md':100*1024}
    line=('Synthetic arithmetic audit: gcd(a,b)=gcd(b,a mod b). '
          'Unproved generalizations remain gaps. Test pair 35,21 gives gcd 7.\n')
    for name,size in sizes.items():
        path=Path(work)/name
        if path.is_symlink():raise ValueError('fixture symlink refused')
        existing=path.stat().st_size if path.exists() else 0
        if existing>=size:continue
        if name=='NOTES.md':
            try:data=json.loads(path.read_text()) if path.exists() else {}
            except (ValueError,UnicodeError):data=None
            if isinstance(data,dict):
                # Retain every model-authored field, keeping the six-field oracle readable.
                key='_synthetic_history'
                while key in data:key+='_'
                data[key]=''
                count=max(0,size-len(json.dumps(data)))
                data[key]=(line.replace('\n',' ')*((count//len(line))+1))[:count]
                path.write_text(json.dumps(data))
                continue
        with path.open('a') as out:out.write(('\n'+line*((size-existing)//len(line)+1))[:size-existing])
