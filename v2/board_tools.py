"""Worker-only board MCP transport, executable from the immutable flat release."""
import base64
import hashlib
import json
import re
try: from .format_json import loads, refs
except ImportError: from format_json import loads, refs
from pathlib import Path
import socket

LIMIT = 3*1024*1024
OPERATIONS = ['post','post_initial','post_note','post_help','read_record','wire_records',
              'read_artifact','heartbeat','post_result','withdraw','changes',
              'post_claim','post_finding','release_claim','post_comment','open_claims','planner_page','post_plan','review_page']
POST_HELP = ('To share anything, post markdown: operation "post", arguments {"text": "..."}. Optional first lines are '
    'read leniently: "Post-type: reasoning", "Status: verified | computation-checked | unverified | refuted" with "Task: ..." records a '
    'finding; "Claim: <task>" announces what you are working on; "Release: <task>" drops a claim; "Re: <record id>" '
    'replies to a record. Every result claim needs Post-type: reasoning or literature. An unlabelled post that is not a verdict is a note. Verdicts use Post-type: verdict, Status: ACCEPT | REJECT | objection and Re: <exact revision record id>. '
    'Certifier/tie-break REJECT on reasoning requires Proposed-repair: followed by Corrected-statement: and Corrected-proof:, or Exact-gap: and Repair-route:. The board posts the repair as an unverified reasoning revision authored by you. ')
TOOL = dict(name='board_call', description=POST_HELP+'Other operations stay available (? = optional): '
    'review_page(page?) for the complete bound revision/evidence; read_record(record_id); wire_records(record_id); read_artifact(artifact_hash); changes(after?, max_tokens?); open_claims(); '
    'heartbeat(key, assignment_version); post_result(key, assignment_version, claim, scope, content, limitations, pointers, coauthors?, claim_type?) '
    'with key and version from ASSIGNMENT.json; post_initial(approach, assumptions, target, content); post_note(kind, scope, evidence, dependencies?); '
    'post_help(obligation, contribution, completion_condition, dependencies, milestone_id?); withdraw(record_id, evidence); '
    'post_claim(task, plan?); post_finding(task, status, statement, evidence, verified_by?, post_type (reasoning or literature for claims), revision_of?, literature?, proposed_repair? (same repair field text for REJECT)); release_claim(task, reason?); post_comment(text, about?). '
    'A structured write the board cannot accept is kept as a markdown note and you get a warning.',
    inputSchema=dict(type='object', properties={'operation':{'type':'string','description':'default "post"'},
        'arguments':{'type':['object','string'],'description':'object (JSON text is also read)'},'text':{'type':'string','description':'shortcut for operation "post"'}}))
# p01/p06 certifiers (2026-10-11): the client's own schema check refused a board_post that nested
# {"text": ...} inside "arguments" and a board_call whose arguments were JSON text, before the board
# saw either. Both shapes are unambiguous, so the schemas admit them and main() unwraps them.
POST_TOOL = dict(name='board_post', description='Post markdown to the shared board. '+POST_HELP,
    inputSchema=dict(type='object', properties={'text':{'type':'string'}}))


# Live check 2026-10-07 (p02 open-60): 72 of 770 tool replies were errors, 60 of them board_call
# argument shapes the board refused although the intent was plain: the operation nested inside
# "arguments" (TypeError "multiple values for argument 'operation'"), named by another key
# (op/action/cmd/method/...), passed as {"changes": {...}}, or put in "text" ("read_record"),
# which was silently POSTED as a note 22 times and drove two members into loop detection.
_OP_KEYS = ('operation', 'op', 'action', 'cmd', 'command', 'method', 'board_op', 'op_name', 'operation_name')
_OP_WORDS = set(OPERATIONS) | {'read', 'get_record', 'list_claims', 'note', 'comment', 'claim', 'finding', 'release',
                               'post_markdown', 'write', 'add_note'}
_READ_OPS = {'changes', 'open_claims', 'read_record', 'wire_records', 'read_artifact', 'heartbeat','planner_page','review_page'}
_NESTED = ('arguments', 'args', '$args', 'params', 'parameters')
_RECORD_KEYS = ('record_id', 'id', 'record')
_TEXTISH = ('text', 'body', 'comment', 'note', 'message', 'content_text', 'content', 'statement')
_RECORD_RE = re.compile(r'^\s*(?:board\s*:|record\s*|#)?\s*(\d+)\s*$', re.I)
ASSIGNMENT = Path('/work/ASSIGNMENT.json')


def _op_word(value, words=_OP_WORDS):
    if isinstance(value, str) and value.strip().lower().replace('-', '_') in words:
        return value.strip().lower().replace('-', '_')
    return None


def _as_dict(value):
    if isinstance(value, str):
        try:
            parsed = loads(value)
            return parsed if isinstance(parsed, dict) else {'text': value}
        except (ValueError, SyntaxError):
            return {'text': value}
    if isinstance(value, list) and all(isinstance(x, (list, tuple)) and len(x) == 2 for x in value):
        return dict(value)
    if value is not None and not isinstance(value, dict):
        return {'text': value if isinstance(value, str) else json.dumps(value, default=str)}
    return dict(value or {})


def _record_number(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _RECORD_RE.match(value)
        if match:
            return int(match.group(1))
    return value


def _filled(value):
    return bool(value.strip()) if isinstance(value, str) else value is not None


def _assignment(path=None):
    try:
        data = json.loads(Path(path or ASSIGNMENT).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _normalize(params, assignment_path=None):
    """Operator ruling 2026-10-06: accept any argument shape; only empty/oversize is refused."""
    params = dict(params or {})
    top = None
    for key in ('operation', 'op'):
        value = params.pop(key, None)
        if top is None and value is not None and str(value).strip():
            top = str(value).strip()
    raw = params.pop('arguments', None)
    if raw is None:raw = params.pop('args', None)
    arguments = _as_dict(raw)
    for key, value in params.items():arguments.setdefault(key, value)  # flattened arguments
    nested = None
    for _ in range(3):  # {"arguments": {"arguments": {...}}}, {"changes": {...}} and friends
        if len(arguments) == 1:
            (key, value), = arguments.items()
            same = top is not None and _op_word(key) == top.lower().replace('-', '_')
            if _op_word(key) and (value is None or isinstance(value, dict)) and (same or (top is None and nested is None)):
                nested, arguments = _op_word(key), dict(value or {})
        inner = [k for k in _NESTED if k in arguments and isinstance(arguments[k], (dict, str))]
        if not inner:break
        for key in inner:
            for k, v in _as_dict(arguments.pop(key)).items():arguments.setdefault(k, v)
    for key in _OP_KEYS:
        if key in arguments and _op_word(arguments[key]):
            word = _op_word(arguments.pop(key))
            nested = nested or word
    if 'operation' in arguments:  # never a board argument: it would collide with the call's own
        value = arguments.pop('operation')
        if nested is None and isinstance(value, str) and value.strip():
            nested = value.strip()
    if 'kind' in arguments and _op_word(arguments['kind'], _READ_OPS) and not any(_filled(arguments.get(k)) for k in _TEXTISH):
        nested = nested or _op_word(arguments.pop('kind'))
    # {"text": "read_record", "arguments": {"record_id": 10}} names a read; an explicit post
    # (board_post, or operation "post") of the same word stays a post.
    word = _op_word(arguments.get('text'), _READ_OPS | {'read'})
    if top is None and nested is None and word and (word not in ('read_record', 'wire_records', 'read') or any(k in arguments for k in _RECORD_KEYS)):
        nested = word; arguments.pop('text')
    has_text = any(_filled(arguments.get(k)) for k in _TEXTISH)
    if top is None or (top.lower() == 'post' and nested and not has_text):
        operation = nested
    else:
        operation = top
    if operation is None and not has_text:
        if any(k in arguments for k in _RECORD_KEYS):
            operation = 'read_record'
        elif arguments and set(arguments) - {'text'} <= {'after', 'max_tokens', 'cursor', 'since'}:
            operation = 'changes'
    operation = (operation or 'post').strip()
    canon = operation.lower().replace('-', '_')
    canon = {'read': 'read_record', 'get_record': 'read_record', 'list_claims': 'open_claims'}.get(canon, canon)
    if canon in OPERATIONS:operation = canon
    if operation.lower() != 'post' and 'text' in arguments and not _filled(arguments['text']):
        arguments.pop('text')  # an empty "text" beside a read is noise, not a post
    for key in _RECORD_KEYS:
        if key in arguments:
            arguments[key] = _record_number(arguments[key])
    # turn audit 2026-10-11: planners sent plan ids as "126", "#126" or "board:126" (11 refusals);
    # the same forms are read as record numbers everywhere else.
    if operation == 'post_plan' and isinstance(arguments.get('plan'), dict):
        for key in ('established', 'conditional', 'blockers', 'select'):
            if isinstance(arguments['plan'].get(key), list):
                arguments['plan'][key] = [_record_number(x) for x in arguments['plan'][key]]
    if operation in ('read_record', 'wire_records', 'read') and not any(k in arguments for k in _RECORD_KEYS):
        number = _record_number(arguments.get('text'))
        if isinstance(number, int):
            arguments.pop('text'); arguments['record_id'] = number
    if 'cursor' in arguments and 'after' not in arguments and operation == 'changes':
        arguments['after'] = arguments.pop('cursor')
    if operation in ('heartbeat', 'post_result'):
        lease = _assignment(assignment_path)
        if not any(k in arguments for k in ('key', 'lease_key', 'assignment_key')) and lease.get('key'):
            arguments['key'] = lease['key']
        if not any(k in arguments for k in ('assignment_version', 'version')) and lease.get('assignment_version') is not None:
            arguments['assignment_version'] = lease['assignment_version']
    for field in ('premise_refs', 'literature_refs'):
        if field in arguments:
            arguments[field] = refs(arguments[field])
    if 'literature' in arguments and isinstance(arguments['literature'], str):
        arguments['literature'] = loads(arguments['literature'])
    return operation, arguments


# Live check 2026-10-07 (p02 rdcheck2 mixed): critics sent bare command texts ("read_record 87",
# "wire_records 41", "post_result") that were posted to the board as notes. Operator rule: never
# drop them silently. A text that parses as a complete board call is executed; any other bare
# command text is appended to the member's own NOTES.md (never the board) and logged for the
# harness, which journals it as an event.
WORK = Path('/work')
COMMAND_LOG = '.harness-command-texts.jsonl'
_COMMAND_RE = re.compile(r'^\s*(?:board_call|board_post)?\s*[:\s]?\s*([A-Za-z_][\w-]*)\s*(?:\(\s*(.*?)\s*\)|\s+(.*?))?\s*;?\s*$', re.S)
_NO_ARG_OPS = {'changes', 'open_claims'}
_RECORD_OPS = {'read_record', 'wire_records', 'read', 'get_record'}
_COMMAND_WORDS = set(OPERATIONS) - {'post'} | {'read', 'get_record', 'list_claims', 'board_call', 'board_post'}


def command_text(text):
    """(operation, arguments) when a text is a complete board call; ('', None) when it is a bare
    command that is not a complete call; None when it is ordinary prose."""
    if not isinstance(text, str) or len(text) > 200 or '\n' in text.strip():
        return None
    match = _COMMAND_RE.match(text)
    if not match:
        return None
    word = match.group(1).lower().replace('-', '_')
    if word not in _COMMAND_WORDS:
        return None
    rest = (match.group(2) if match.group(2) is not None else match.group(3) or '').strip()
    word = {'read': 'read_record', 'get_record': 'read_record', 'list_claims': 'open_claims'}.get(word, word)
    if word in ('board_call', 'board_post'):
        return ('', None)
    if word in _RECORD_OPS | {'read_record'}:
        bare = re.sub(r'^(?:record_id|id|record)\s*[=:]\s*', '', rest, flags=re.I)
        number = _record_number(bare)
        if isinstance(number, int):
            return (word, {'record_id': number})
        return ('', None) if not rest else None
    if word in _NO_ARG_OPS:
        if not rest:
            return (word, {})
        if word == 'changes':
            m = re.match(r'^(?:after|cursor)?\s*[=:]?\s*(\d+)$', rest)
            if m:
                return (word, {'after': int(m.group(1))})
        return None  # a command word followed by prose is prose
    if rest.startswith('{'):
        try:
            parsed = loads(rest)
            if isinstance(parsed, dict):
                return (word, parsed)
        except (ValueError, SyntaxError):
            pass
        return ('', None)
    # Bare post_result / heartbeat / post_claim ... with no arguments: a command, not a call.
    return ('', None) if not rest else None


def keep_command_text(text, work=None, reason='bare-command'):
    """Append an unexecutable command text to the member's NOTES.md and the harness log."""
    import time
    work = Path(work or WORK)
    try:
        with open(work / 'NOTES.md', 'a') as notes:
            notes.write('\n\n[board tool: this text was sent as a board post but reads as a tool command, so it was '
                        'kept here instead of posted]\n' + text.strip() + '\n')
    except OSError:
        pass
    log_command_text(text, 'kept-in-notes', work=work, reason=reason)


def log_command_text(text, action, work=None, **extra):
    import time
    try:
        with open(Path(work or WORK) / COMMAND_LOG, 'a') as log:
            log.write(json.dumps(dict(at=time.time(), action=action, text=text[:200], **extra)) + '\n')
    except OSError:
        pass


def collect_command_texts(workers, seen):
    """Harness side: new command-text log lines from member workdirs as harness events."""
    out = []
    for w in workers:
        work = getattr(w, 'work', None)
        if work is None:
            continue
        path = Path(work) / COMMAND_LOG
        try:
            size = path.stat().st_size
        except OSError:
            continue
        key = str(path); start = seen.get(key, 0)
        if size <= start:
            continue
        with open(path, 'rb') as fh:
            fh.seek(start); chunk = fh.read(size - start)
        end = chunk.rfind(b'\n') + 1
        seen[key] = start + end
        for line in chunk[:end].splitlines():
            try:
                row = json.loads(line)
            except (ValueError, SyntaxError):
                continue
            out.append(dict(kind='command-text-' + str(row.pop('action', 'logged')), slot=getattr(w, 'slot_id', None), **row))
    return out


NOTES_SEED = ('# Notes\n\nYour own working notes for this task (created empty by the harness). '
              'Write what you have done, what you found and what comes next.\n')
EXPERIMENTS_SEED = ('# Experiments\n\nYour own log of computations and checks (created empty by the harness).\n')


def seed_member_files(work, brief=''):
    """Create the member-owned files a brief tells it to read, when absent (never overwrite)."""
    work = Path(work)
    wanted = {'NOTES.md': NOTES_SEED}
    if 'EXPERIMENTS.md' in (brief or ''):
        wanted['EXPERIMENTS.md'] = EXPERIMENTS_SEED
    for name, text in wanted.items():
        path = work / name
        try:
            if not path.exists():
                work.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        except OSError:
            pass


def _command_redirect(operation, arguments):
    """A post whose whole text is a command: (operation, arguments) to run instead, or 'kept'."""
    if operation != 'post' or set(k for k in arguments if _filled(arguments.get(k))) - {'text', 'about', 'format', 'role'}:
        return None
    parsed = command_text(arguments.get('text'))
    if parsed is None:
        return None
    if parsed[1] is None:
        keep_command_text(arguments['text'])
        return 'kept'
    log_command_text(arguments['text'], 'executed', operation=parsed[0])
    return parsed


def call(operation=None, arguments=None, **extra):
    operation, arguments = _normalize(dict(extra, operation=operation, arguments=arguments))
    redirect = _command_redirect(operation, arguments)
    if redirect == 'kept':
        return json.dumps(dict(posted=False, kept_in='NOTES.md', note='This text reads as a tool command, not a post, '
            'so it was appended to your NOTES.md instead of the board. To run a board operation use board_call with '
            'operation and arguments, e.g. {"operation": "read_record", "arguments": {"record_id": 1}}.'))
    if redirect:
        operation, arguments = redirect
    credentials = json.loads(Path('/harness-control/board-client.json').read_text())
    if 'content' in arguments:
        content = arguments['content']
        if not isinstance(content, str):content = json.dumps(content, sort_keys=True)
        arguments['content'] = {'$bytes':base64.b64encode(content.encode()).decode()}
    raw = json.dumps(dict(credentials, operation=operation, arguments=arguments)).encode()+b'\n'
    if len(raw)>LIMIT:raise ValueError('board request is %d bytes; the limit is %d. Split it into several posts.' % (len(raw), LIMIT))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(15)
        connection.connect('/harness-control/board.sock')
        connection.sendall(raw)
        with connection.makefile('rb') as stream:line=stream.readline(LIMIT+1)
    if len(line)>LIMIT or not line.endswith(b'\n'):raise ValueError('board response exceeds bound')
    response=json.loads(line)
    if not response.get('ok'):return json.dumps(correction(response.get('error','board unavailable'), operation, arguments))
    return json.dumps(response['result'])


_REFUSALS = {}

def correction(error, operation=None, arguments=None):
    """A refusal is a workflow result, never an admission or fabricated evidence.

    In particular do not suggest removing a dependency to pass an admission gate.
    A model must decide its declarations from its own work and actual board state.
    """
    error = str(error)
    key = hashlib.sha256(json.dumps([error, operation, arguments], sort_keys=True, default=str).encode()).hexdigest()
    attempt = _REFUSALS.get(key, 0) + 1
    if len(_REFUSALS) >= 128: _REFUSALS.pop(next(iter(_REFUSALS)))
    _REFUSALS[key] = attempt
    next_call = None
    if 'no bound evidence view' in error:
        action = ('This session has no bound review packet. Read the record IDs in ASSIGNMENT.json '
                  'with read_record or wire_records. review_page is only for a harness-bound review; '
                  'it cannot create a binding. Do not retry review_page in this session.')
        next_call = {'operation': 'changes', 'arguments': {}}
    elif 'Premise-refs required' in error:
        action = ('Add Premise-refs: <explicit array of ALL dependency revision IDs> to your reasoning post '
                  'or premise_refs to post_finding. Declare [] only if your work has no dependencies. '
                  'Never remove a dependency to obtain admission. Resubmit the same content with the declaration.')
    elif 'current revision' in error or 'exact source revisions' in error:
        action = ('Read the current board revisions and their acceptance states before resubmitting. '
                  'Keep every actual dependency; do not omit an unaccepted dependency to pass. '
                  'If a required premise is unaccepted, preserve the work in NOTES.md or a Post-type: note '
                  'and wait for acceptance. A note receives no claim, review or certification credit.')
        next_call = {'operation': 'changes', 'arguments': {}}
    elif any(x in error for x in ('Expecting', 'Extra data', 'Invalid \\escape', 'JSON', 'literal', 'refs requires')):
        action = ('Send arguments as an object. For markdown metadata use one-line JSON: '
                  'Premise-refs: [<exact IDs>] and Literature: <object>. Escape literal backslashes as '
                  '\\\\; keep quotes, hashes, version IDs and source spans from your own evidence. '
                  'Do not guess missing metadata. Preserve the attempted content in NOTES.md if it cannot be corrected.')
    else:
        action = ('Correct the field named in the refusal using your own recorded evidence. '
                  'Read the relevant record with read_record if needed. If the required evidence or authority '
                  'is unavailable, preserve the content in NOTES.md or as Post-type: note; a note is not '
                  'an admitted result. Do not repeat the unchanged request.')
    if attempt > 1:
        action += (' The unchanged request has already been refused. Stop resubmitting it; '
                   'save the work in NOTES.md and perform the indicated read/correction first. '
                   'If the prerequisite cannot be satisfied in this session, end the turn with the limitation.')
    return dict(accepted=False, marker='board request denied', refusal_count=attempt, error=error, correction=action, next_call=next_call,
                retry='only after correcting the input or satisfying the stated prerequisite')


def main():
    import sys
    for line in sys.stdin:
        if len(line)>LIMIT:raise ValueError('MCP request exceeds bound')
        request=json.loads(line)
        if 'id' not in request:continue
        refusal_operation=None; refusal_arguments=None
        try:
            refusal_arguments=(request.get('params') or {}).get('arguments')
            method=request['method']
            if method=='initialize':
                result=dict(protocolVersion='2024-11-05',capabilities={'tools':{}},serverInfo={'name':'board','version':'1'})
            elif method=='tools/list':result={'tools':[TOOL,POST_TOOL]}
            elif method=='tools/call':
                params=request['params']
                args=params.get('arguments') or {}
                if isinstance(args,str):  # arguments sent as JSON text: parse (fix 2026-10-07)
                    try:
                        parsed=loads(args)
                        if isinstance(parsed,str):parsed=loads(parsed)
                        if isinstance(parsed,dict):args=parsed
                    except (ValueError, SyntaxError):pass
                if not isinstance(args,dict):args={'text':args if isinstance(args,str) else json.dumps(args,default=str)}
                if params.get('name')=='tool_call':  # bridge call sent here directly: unwrap
                    try:from .tool_repair import repair_call
                    except ImportError:from tool_repair import repair_call  # flat release (/harness)
                    name,inner=repair_call('tool_call',args,[TOOL,POST_TOOL])
                    if name in ('board_call','board_post') and isinstance(inner,dict):params,args=dict(params,name=name),inner
                    else:params=dict(params,name=None)
                if not params.get('name'):  # missing tool name: infer from the argument keys
                    params=dict(params,name='board_post' if set(args)<={'text'} else 'board_call')
                if params.get('name')=='board_post' and 'text' not in args and any(k in args for k in ('operation',)+_NESTED):
                    params=dict(params,name='board_call')  # a board_call shape sent to board_post
                if params.get('name')=='board_post':
                    if not isinstance(args.get('text'),str) or not args['text'].strip():
                        raise ValueError('board_post needs {"text": "<markdown>"}; for a verdict the text starts:\n'
                                         'Post-type: verdict\nStatus: ACCEPT   (or REJECT, or objection)\nRe: <record id>')
                    args=dict(operation='post',arguments=dict(args))
                elif params.get('name')!='board_call':raise ValueError('unknown tool %r: use board_post {text} or board_call' % params.get('name'))
                refusal_operation=args.get('operation'); refusal_arguments=args.get('arguments',args)
                result={'content':[{'type':'text','text':call(**args)}]}
            elif method=='ping':result={}
            else:raise ValueError('unknown method')
            response=dict(jsonrpc='2.0',id=request['id'],result=result)
        except Exception as exc:  # any failure is a tool error; the MCP process never dies (review #2)
            response=dict(jsonrpc='2.0',id=request['id'],result={'content':[{'type':'text','text':json.dumps(correction(str(exc), refusal_operation, refusal_arguments))}]})
        print(json.dumps(response),flush=True)

if __name__=='__main__':main()
