"""Authenticate source quotations against controller-retained source bytes."""
import hashlib
import json
import re
from pathlib import Path
from .verification_tools import text_sources


def authenticate(card, root):
    if not isinstance(card,dict) or root is None:return False
    root=Path(root)
    try:
        receipts=[]
        for path in root.glob('*.json'):
            if path.name.endswith('.failure'):continue
            try:
                meta=json.loads(path.read_text());body=path.with_suffix('.body').read_bytes()
                if hashlib.sha256(body).hexdigest()==meta.get('sha256'):receipts.append((meta,body))
            except (OSError,ValueError):continue
        for source in card.get('sources',[]):
            matches=[(m,b) for m,b in receipts if m['sha256']==source['sha256'] and m['url'].rstrip('/').endswith('/'+source['id'])]
            if not matches:return False
            matched=False
            for _,body in matches:
                for part in text_sources(body):
                    location=source['location']
                    if not (location==part['location'] or location.startswith(part['location']+' ')):continue
                    text=part['text'];lines=re.search(r'lines?\s+(\d+)(?:\s*[-–:]\s*(\d+))?',location)
                    if lines:
                        a=int(lines.group(1));b=int(lines.group(2) or a)
                        text='\n'.join(text.splitlines()[a-1:b])
                    norm=lambda s:' '.join(s.split())
                    if norm(source['quote']) in norm(text):matched=True
            if not matched:return False
        # Search/access rows require actual controller receipts; raw declarations\n        # never establish a global absence result or an access failure.
        for search in card.get('searches',[]):
            if not any(m.get('query')==search['query'] and search['result'] in b.decode(errors='replace') for m,b in receipts):return False
        for access in card.get('accesses',[]):
            key=hashlib.sha256(access['url'].encode()).hexdigest()
            failure=json.loads((root/(key+'.failure')).read_text())
            if failure.get('url')!=access['url'] or failure.get('error')!=access['error']:return False
        return bool(card.get('sources') or card.get('searches') or card.get('accesses'))
    except (OSError,ValueError,KeyError,TypeError):return False


def retained_sources(card, root):
    """Full decompressed sources behind an authenticated card, for reviewer paging."""
    if not authenticate(card,root):return []
    rows=[]
    for source in card.get('sources',[]):
        for path in Path(root).glob('*.json'):
            try:
                meta=json.loads(path.read_text());data=path.with_suffix('.body').read_bytes()
                if meta.get('sha256')==source['sha256'] and hashlib.sha256(data).hexdigest()==source['sha256']:
                    rows.extend(text_sources(data));break
            except (OSError,ValueError):continue
    return rows
