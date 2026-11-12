"""Finite filesystem MCP tools for restricted roles; no shell, CAS or retrieval.

Executed from an immutable release inside the worker namespace. Paths and byte
limits are service policy, not model-supplied capabilities.
"""
import json
import fcntl
import os
from pathlib import Path
import sys

LIMIT = 1024 * 1024
# Frozen evidence snapshots allow 640 MiB in total. A controller packet may
# exceed the report-output limit (P07: the claim manifest occurs twice in it).
# Keep a separate finite file limit; replies still use 20,000-character pages.
EVIDENCE_LIMIT = 64 * 1024 * 1024
REPORT_FILES = {'FINAL.tex', 'PROVENANCE.md', 'AUDIT.md', 'BLOCKED.md'}
ROLES = {'report-author', 'internal-verifier', 'peer-reviewer', 'specialist', 'orchestrator'}


def read_bounded(root, relative, *, max_bytes=LIMIT):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('relative evidence path required')
    # Open every component without following symlinks, including parent dirs.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            import stat
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise ValueError('bounded regular evidence file required')
            data = os.read(source, max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError('evidence read exceeds bound')
            return data.decode()
        finally:
            os.close(source)
    finally:
        os.close(fd)


PAGE_CHARS = 20000


def page(text, offset=0, limit=None, tool='read_evidence'):
    limit = PAGE_CHARS if not isinstance(limit, int) or limit < 1 else min(limit, PAGE_CHARS)
    n = len(text)
    if offset == 0 and n <= limit:
        return text
    if offset >= n:
        return '[end of file: %d characters; offset %d is past the end]' % (n, offset)
    end = min(n, offset + limit)
    nl = text.rfind('\n', offset, end)
    if end < n and nl > offset + limit // 2:
        end = nl + 1  # end pages on a line boundary when possible
    head = '[showing characters %d-%d of %d' % (offset, end, n)
    head += ('; next page: call %s with offset=%d]\n' % (tool, end)) if end < n else '; end of file]\n'
    return head + text[offset:end]


# A report author may inspect a bounded amount before checkpointing its own draft.
# The final page may cross this threshold by at most PAGE_CHARS plus its header.
FIRST_SAVE_CHARS = 60000
FIRST_SAVE_NOTICE = ('Evidence reads paused until the first draft is saved. Use write_report '
                     'with name=FINAL.tex to save your current draft, even if incomplete; '
                     'then evidence reads resume. Keep unverified material labelled as such. '
                     'The harness does not supply or approve the draft content.')


def writer_first_read(output, reply):
    try:
        if read_bounded(output, 'FINAL.tex').strip():return reply
    except FileNotFoundError:
        pass
    fd = os.open(Path(output)/'.writer-first-read-chars', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_nlink != 1:raise ValueError('read budget hardlink refused')
        raw = os.read(fd, 128)
        used = int(raw) if raw else 0
        if used < 0:raise ValueError('invalid writer read budget')
        if used >= FIRST_SAVE_CHARS:return FIRST_SAVE_NOTICE
        os.lseek(fd, 0, os.SEEK_SET);os.ftruncate(fd, 0)
        os.write(fd, str(used + len(reply)).encode());os.fsync(fd)
        return reply
    finally:
        os.close(fd)


def tools_for(role):
    if role not in ROLES:
        raise ValueError('unknown restricted role')
    if role == 'orchestrator':return []
    tools = [dict(name='read_evidence', description=('Read one file from frozen evidence, at most %d characters per call. '
                  'Longer files are returned in pages: the reply says which characters it shows and the offset of the next page; '
                  'call again with that offset to continue.' % PAGE_CHARS) +
                  (' Before the first nonempty FINAL.tex save, at most about 80,000 characters of evidence are available; save a draft to resume reads.' if role == 'report-author' else ''),
                  inputSchema=dict(type='object', properties={'path': {'type': 'string'},
                      'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': PAGE_CHARS}},
                      required=['path'], additionalProperties=False))]
    if role == 'report-author':
        tools.append(dict(name='read_report', description='Read one current report file, in pages of at most %d characters (use offset for the next page).' % PAGE_CHARS, inputSchema=dict(type='object', properties={'name': {'type': 'string', 'enum': sorted(REPORT_FILES)}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': PAGE_CHARS}}, required=['name'], additionalProperties=False)))
        tools.append(dict(name='write_report', description='Replace one report file with bounded UTF-8 text.',
                          inputSchema=dict(type='object', properties={'name': {'type': 'string', 'enum': sorted(REPORT_FILES)}, 'text': {'type': 'string'}}, required=['name', 'text'], additionalProperties=False)))
    if role in ('internal-verifier', 'peer-reviewer'):
        tools.append(dict(name='retrieve_source', description='Retrieve cited arXiv LaTeX source first, HTML fallback; returns an authenticated whole-source receipt, bounded text and an omission index. Request a location/start/limit to inspect other slices of the retained source.',
                          inputSchema=dict(type='object',properties={'url':{'type':'string'},'location':{'type':'string'},'start':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':16000}},required=['url'],additionalProperties=False)))
        tools.append(dict(name='bounded_check', description='Run a bounded Python reproduction in private verifier scratch, no candidate editing.',
                          inputSchema=dict(type='object',properties={'program':{'type':'string'}},required=['program'],additionalProperties=False)))
        tools.append(dict(name='submit_review', description='Write this verifier session review; cannot edit candidate.',
                          inputSchema=dict(type='object', properties={'text': {'type': 'string'}}, required=['text'], additionalProperties=False)))
    return tools


def consume_read(output, limit):
    if type(limit) is not int or not 0 <= limit <= 16:
        raise ValueError('invalid restricted read allowance')
    fd=os.open(Path(output)/'.evidence-read-budget.json',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        if os.fstat(fd).st_nlink!=1: raise ValueError('read budget hardlink refused')
        raw=os.read(fd,1024)
        used=json.loads(raw) if raw else 0
        if type(used) is not int or used<0 or used>=limit:
            raise ValueError('finite evidence-read allowance exhausted; current report remains readable')
        os.lseek(fd,0,os.SEEK_SET);os.ftruncate(fd,0)
        os.write(fd,str(used+1).encode());os.fsync(fd)
    finally:
        os.close(fd)


def invoke(role, name, args, *, evidence='/evidence', output='/role-output', read_limit=None):
    if name not in {x['name'] for x in tools_for(role)}:
        raise ValueError('tool forbidden for role')
    if name == 'retrieve_source':
        import urllib.request
        data=json.dumps({k:args[k] for k in ('url','location','start','limit') if k in args}).encode()
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8877/verification-source',data=data,headers={'Content-Type':'application/json'}),timeout=30) as response:
            return response.read(8*LIMIT).decode()
    if name == 'bounded_check':
        import subprocess, tempfile, resource, hashlib
        program=args['program']
        if not isinstance(program,str) or len(program.encode())>65536:raise ValueError('bounded program required')
        def limits():
            resource.setrlimit(resource.RLIMIT_CPU,(5,5))
            resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
            resource.setrlimit(resource.RLIMIT_FSIZE,(LIMIT,LIMIT))
            resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
            # Checking is one process: descendants must not outlive its wall
            # timeout or multiply its per-process CPU/memory allowances.
            resource.setrlimit(resource.RLIMIT_NPROC,(0,0))
        with tempfile.TemporaryDirectory(prefix='verification-') as scratch:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                result=subprocess.run([sys.executable,'-I','-c',program],cwd=scratch,env={'PATH':'/usr/bin:/bin'},stdout=out,stderr=err,timeout=10,preexec_fn=limits)
                out.seek(0);err.seek(0)
                return json.dumps(dict(program_sha256=hashlib.sha256(program.encode()).hexdigest(),returncode=result.returncode,stdout=out.read(LIMIT).decode(errors='replace'),stderr=err.read(LIMIT).decode(errors='replace')))
    if name == 'read_evidence':
        offset = args.get('offset') or 0
        if not isinstance(offset, int) or offset < 0:raise ValueError('offset must be a non-negative integer')
        # Live 23:10: tool results over 25,000 characters reach the model as a short preview,
        # so long evidence is served in pages. Only the first page counts against a read budget.
        if read_limit is not None and args['path'] not in ('BUILD.json','COMPILER-DIAGNOSTICS.txt'):
            # Review: the budget counts files, not pages; any page of a file not yet opened
            # in this session consumes one read.
            seen_path = Path(output) / '.evidence-paths.json'
            try:opened = set(json.loads(seen_path.read_text()))
            except (OSError, ValueError):opened = set()
            if args['path'] not in opened:
                consume_read(output,read_limit)
                opened.add(args['path']); seen_path.write_text(json.dumps(sorted(opened)))
        reply = page(read_bounded(evidence, args['path'], max_bytes=EVIDENCE_LIMIT), offset, args.get('limit'))
        return writer_first_read(output, reply) if role == 'report-author' else reply
    if name == 'read_report':
        if args['name'] not in REPORT_FILES: raise ValueError('report path refused')
        return page(read_bounded(output, args['name']), args.get('offset') or 0, args.get('limit'), tool='read_report')
    filename = args['name'] if name == 'write_report' else ('VERIFICATION.json' if role == 'internal-verifier' else 'review.json')
    if name == 'write_report' and filename not in REPORT_FILES:
        raise ValueError('report path refused')
    text = args['text']
    if not isinstance(text, str) or len(text.encode()) > LIMIT:
        raise ValueError('write exceeds bound')
    # Output is a private role/session directory. No pathname from evidence.
    fd = os.open(Path(output)/filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise
    return 'recorded'


def main():
    role = os.environ['HARNESS_ROLE']
    raw_limit=os.environ.get('HARNESS_EVIDENCE_READ_LIMIT','')
    read_limit=int(raw_limit) if raw_limit else None
    for line in sys.stdin:
        if len(line) > 2 * LIMIT:
            raise ValueError('MCP input exceeds bound')
        request = json.loads(line)
        if 'id' not in request:
            continue
        try:
            method = request['method']
            if method == 'initialize':
                result = dict(protocolVersion='2024-11-05', capabilities={'tools': {}}, serverInfo={'name': 'role-files', 'version': '1'})
            elif method == 'tools/list':
                result = {'tools': tools_for(role)}
            elif method == 'tools/call':
                params = request['params']
                try:from .tool_repair import repair_call
                except ImportError:from tool_repair import repair_call  # flat release (/harness)
                name, arguments = repair_call(params.get('name'), params.get('arguments', {}), tools_for(role))
                result = {'content': [{'type': 'text', 'text': invoke(role, name, arguments, read_limit=read_limit)}]}
            elif method == 'ping':
                result = {}
            else:
                raise ValueError('unknown method')
            response = dict(jsonrpc='2.0', id=request['id'], result=result)
        except (ValueError, KeyError, OSError) as exc:
            response = dict(jsonrpc='2.0', id=request['id'], error={'code': -32602, 'message': str(exc)})
        print(json.dumps(response), flush=True)

if __name__ == '__main__':
    main()
