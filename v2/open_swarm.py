"""Open swarm: no orchestrator (operator ruling ~18:15 PDT).

Research: every member runs continuously as its own worker session, coordinating
only through the shared notes (board claims, findings, comments). A member whose
session ends is relaunched until the research stop.

Closing, in four phases inside the closing window (fractions in the profile):
write (each full member drafts its own report), critique (every member, the light
member included, critiques every other report), rewrite (each full member revises
using the critiques it received), vote (every member votes for the best report not
its own; most votes wins, tie -> most claims labelled verified). All four reports
are compiled and kept; the winner is the arm's submitted report.

These functions are mixed into SwarmRuntime; `self` is the runtime.
"""
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .control import atomic_json

MEMBER_BRIEF = (
    'You are a member of an open research team working on PROBLEM.md. There is no coordinator; the team '
    'coordinates only through the shared notes (the board, via board_call).\n'
    "Read the notes. Don't work on something while someone else is (they need to write). Always work on something, "
    'whether it is a new approach, a subtask, or a verification. Write what you are doing and what you need help with.\n'
    'Mechanics: post markdown with board_post {text}. To claim a task, start the post with "Claim: <task>" and your '
    'plan; if someone else already holds that claim your post is kept as a note, so choose another task or verify '
    'theirs. When you finish a task, start the post with "Post-type: reasoning", "Status: verified | computation-checked | unverified | '
    'refuted" and "Task: <task>", then the statement and evidence; this releases your claim. "Release: <task>" drops a '
    'claim; "Re: <record id>" replies to a note; anything else is a plain note (ideas, objections, requests for help). '
    'Re-read the notes (board_call changes, open_claims) before each step. Post progress at least '
    'every ~10 minutes of work and before any long computation (what you found, what you are stuck on, '
    'status), not only at the end. Keep working until your session ends; a fresh session will continue from the notes.')
LIGHT_BRIEF = (
    'You are a LIGHT member of an open research team working on PROBLEM.md, coordinating through the shared notes '
    '(the board). Only read the notes (board_call changes, read_record, open_claims) and post comments with '
    'board_post {text} (markdown; "Re: <record id>" on the first line to reply): ideas, objections, gaps you see, and suggested next steps. Do not post '
    'claims or findings and do not run long computations. Keep each turn short.')
# Operator 2026-10-10: no per-claim status tags or record pointers; proofs written out (WRITER_DOCUMENT).
STATUS_RULE = ('State each result as proved (with its complete proof written out), conditional or conjectural, '
               'following the document rules below.')
WRITE_TASK = ('You are {member}, writing YOUR OWN complete final report for the team, from BOARD.json, '
              'FINAL-FINDINGS.json, PROBLEM.md and, when present, the research record of earlier sessions: PRIOR-SUMMARY.md '
              '(every finding, claim and result with its status) and the PRIOR-WORK-NNN.md parts it names (PRIOR-WORK.md is '
              'the index; read long files in 20,000-character pages with read_evidence\'s offset); in a write-up-only session '
              'treat this record as the board. FIRST, within your first two turns, write a complete draft to '
              'FINAL.tex (the Status section and the statements); then read the full text of each accepted argument you state '
              'and write out its proof, saving after each section. ' + STATUS_RULE)
CRITIQUE_TASK = ('You are {member}. Critique every report in REPORT-*.tex (the other members\' reports). For each, '
                 'write a section headed exactly "## On <member>" listing unsupported claims, wrong status labels, '
                 'gaps and concrete improvements. Write all sections to AUDIT.md. Do not edit any report.')
REWRITE_TASK = ('You are {member}. Revise YOUR report FINAL.tex using CRITIQUES-RECEIVED.md (critiques of your report '
                'by the other members). Fix or downgrade what they found, keep it complete, save after each repair. '
                + STATUS_RULE)
REPAIR_TASK = ('You are a LaTeX repair assistant. FINAL.tex was written by another team member and fails to compile; '
               'LATEX-ERRORS.txt has the compiler output. Fix ONLY the LaTeX so it compiles: markup, environments, '
               'braces, packages, escaping. Do not change any mathematical statement, claim, status label or wording. '
               'Save the repaired FINAL.tex.')
VOTE_TASK = ('You are {member}. Read the final reports REPORT-*.tex. Vote for the best report that is not your own. '
             'Write AUDIT.md with a line "VOTE: <member>" (e.g. "VOTE: dsp") and a short reason.')

from contracts.route import routes as _contract_routes
MODEL_IDS = {k:v[0] for k,v in _contract_routes().items()}


# Role design: one writer and one backup (writer_style, measured 2026-10-11); no all-member write
# and no vote. Critique rounds stay (every member critiques).
from .writer_style import DEFAULT_STYLE, writer_pair
WRITERS = writer_pair('mixed-swarm')
_SR = __import__('v2.swarm_runtime', fromlist=['x'])
WRITER_RULES = _SR.WRITER_DOCUMENT + _SR.WRITER_LABELS + _SR.WRITER_GROUNDING + _SR.WRITER_VERIFIED + DEFAULT_STYLE
BACKUP_NOTE = ' You are the BACKUP writer: your report is used only if the primary writer ({primary}) has no compiled report.'


def _roster(runtime):
    from .roles import roster
    return roster(getattr(runtime, 'strategy', 'open-swarm'))


def writers(runtime):
    r = _roster(runtime)
    # Roster writers must be allowed report authors (closure: config open_members).
    authors = set(runtime.config.get('open_members') or ())
    if r is not None:
        picked = [m for m in dict.fromkeys((r.get('writer'), r.get('backup_writer'))) if m in authors]
        return picked or [m for m in WRITERS if m in authors]
    full, _ = members(runtime)
    return [m for m in WRITERS if m in full]


def is_open(runtime):
    return bool(runtime.config.get('open_swarm'))


def members(runtime):
    """(full, light) member models. With a roster: every roster model once, no light member."""
    from .roles import allowed, roster_slots
    arm = getattr(runtime, 'strategy', 'open-swarm')
    r = _roster(runtime)
    if r is not None and roster_slots(r):
        return list(dict.fromkeys(m for _, m, _ in roster_slots(r))), []
    return ([m for m in runtime.config['open_members'] if allowed(arm, 'members', m)],
            [m for m in runtime.config['open_light'] if allowed(arm, 'members', m)])


def member_slots(runtime):
    """[(name, slot_id, model, role)] for research seeding. name is identifier-safe and
    unique ('ideas-k3', 'critic-k3-2'); without a roster name = model and role = None."""
    from .roles import roster_slots
    r = _roster(runtime)
    if r is not None and roster_slots(r):
        return [('%s-%s' % (role, sid.replace('#', '-')), sid, m, role) for sid, m, role in roster_slots(r)]
    full, light = members(runtime)
    return [(m, m, m, None) for m in full + light]


def member_role(slot_id):
    """Roster role from an open-member assignment id 'open:<name>:<n>', or None."""
    parts = str(slot_id).split(':')
    if len(parts) >= 4 and parts[0] == 'open' and parts[3] in ('ideas', 'critic', 'literature'):
        return parts[3]  # role-mix session: its own role, not its slot's
    if len(parts) >= 3 and parts[0] == 'open':
        role = parts[1].split('-', 1)[0]
        if role in ('ideas', 'critic', 'literature') and '-' in parts[1]:
            return role
    return None


def member_brief(runtime, model, light, role=None):
    from .roles import brief_with_role, slot_brief
    arm = getattr(runtime, 'strategy', 'open-swarm')
    if role:
        brief = slot_brief(MEMBER_BRIEF, arm, model, role)
    else:
        brief = brief_with_role(LIGHT_BRIEF if light else MEMBER_BRIEF, arm, model)
    settings = getattr(runtime, 'swarm_settings', None)
    if settings is None or settings()['state_page']['enabled']:
        from .state_page import BRIEF_LINE
        brief += '\n' + BRIEF_LINE
    from .roles import POST_LINE
    return brief + '\n' + POST_LINE



# ----------------------------------------------------------------- research
def seed(runtime):
    """Keep one live assignment per member during research."""
    if hasattr(runtime, 'closing_only') and runtime.closing_only():
        return  # write-up-only continuation (operator 2026-10-06 19:58)
    stats = runtime.__dict__.setdefault('open_note_stats', {})
    for w in runtime.slots.values():
        rf = w.state.get('board_refresh') or {}
        model = w.run.lane
        prev = stats.get(w.slot_id, {})
        stats[w.slot_id] = dict(member=model, note_digests=rf.get('deliveries', 0), progress_prompts=rf.get('progress_prompts', 0))
    state = runtime.executor.snapshot()
    if state['mode'] != 'research':
        return
    full, light = members(runtime)
    e = runtime.executor
    with e.tx() as (db, s):
        for name, slot_id, model, role in member_slots(runtime):
            branch = 'member-' + name
            live = [a for a in s['assignments'].values() if a['branch'] == branch and a['state'] in ('queued', 'running', 'stopping')]
            if live:
                continue
            n = sum(a['branch'] == branch for a in s['assignments'].values()) + 1
            # Live 11:17: a provider outage made a member die and respawn every minute.
            # Three launches within 15 min back off (10, 20, 40, 60 min), logged.
            now = runtime.clock(); hist = runtime.__dict__.setdefault('open_launches', {}).setdefault(name, [])
            recent = [t for t in hist if now - t < 900]
            if len(recent) >= 3:
                backoff = runtime.__dict__.setdefault('open_backoff', {})
                b = backoff.setdefault(name, dict(level=0, until=0))
                if b['until'] == 0 or (now >= b['until'] and hist[-1] > b['until'] - 1):
                    if now >= b['until']:
                        b['level'] = min(b['level'] + 1, 4); b['until'] = now + min(3600, 600 * 2 ** (b['level'] - 1))
                        runtime.__dict__.setdefault('open_backoff_log', []).append(dict(member=name, at=now, until=b['until'], launches_15min=len(recent)))
                if now < b['until']:
                    continue
            slot_role = role
            if role in ('ideas', 'critic'):
                from .role_mix import decide
                role = decide(runtime, 'open:%s' % name, slot_role, model)
            spec = dict(model=model, role='research', branch=dict(id=branch, approach='open member ' + (
                        '%s %s' % (role, slot_id) if role else model),
                        target='problem', assumptions=[]) if branch not in s['branches'] else branch,
                        brief=member_brief(runtime, model, model in light, role),
                        stance='light' if model in light else (role or 'open'), input_refs=[])
            try:
                e._launch(s, 'open:%s:%d' % (name, n) + (':' + role if role != slot_role else ''), spec, {}, 10 ** 15)
                hist.append(now); del hist[:-20]
            except ValueError:
                pass


# ----------------------------------------------------------------- sessions
def _session(runtime, identity, model, task, files):
    """A closing-role Qwen session in its own directory (never in runtime.slots)."""
    from .worker import QwenWorker
    w = runtime._new_slot(identity, model=model, role='report', stage='open-closing', account='writer',
                          deadline=runtime.deadline, packet={}, brief=task)
    runtime.slots.pop(identity, None)
    for name, data in files.items():
        target = w.work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    from .proposer_controls import write_claims_ledger
    write_claims_ledger(runtime, w.work)
    w.brief += '\nCLAIMS-MANIFEST.json is the sole current authority; bind statements and proofs to its exact claim_id and evidence hashes. Historical seed tables confer no acceptance.'
    if not runtime.accounting.claim_client(w.context):
        return None
    w.run.writeup = True; w.run.phase = 'write-up'; w.state['session'] += 1
    w.state['report_lock'] = dict(ready_at=runtime.clock(), last_tick=runtime.clock(), idle_seconds=0)
    w.state.pop('slot_failure', None); w.state['turn_complete'] = False; w.state['slot_closed'] = False
    w.save()
    gateway = runtime._gateway(w, w.context)
    w.worker = QwenWorker(w.work, w.state['session'], 'write-up', None, lane=model, model=w.context.model,
                          release=runtime.release, gateway=gateway, seconds=max(1, int(runtime.deadline - runtime.clock())),
                          continuous_goal=False, task_text=task)
    runtime.writer_scope.register(w.worker)
    return w


def _restart(runtime, w, task, model):
    """Second session (rewrite) in the same directory."""
    from .worker import QwenWorker
    from .proposer_controls import write_claims_ledger
    write_claims_ledger(runtime, w.work)
    w.brief += '\nCLAIMS-MANIFEST.json is the sole current authority; bind statements and proofs to its exact claim_id and evidence hashes. Historical seed tables confer no acceptance.'
    if not runtime.accounting.claim_client(w.context):
        return False
    w.state['session'] += 1; w.state['turn_complete'] = False; w.state['slot_closed'] = False
    w.state.pop('slot_failure', None); w.save()
    gateway = runtime._gateway(w, w.context)
    w.worker = QwenWorker(w.work, w.state['session'], 'write-up', None, lane=model, model=w.context.model,
                          release=runtime.release, gateway=gateway, seconds=max(1, int(runtime.deadline - runtime.clock())),
                          continuous_goal=False, task_text=task)
    runtime.writer_scope.register(w.worker)
    return True


def _busy(runtime, w):
    return w.worker is not None and (w.state.get('pending') or any(g.active_handlers for g in runtime.gateways if g.watchdog is w))


def _drive_sessions(runtime):
    for w in list(getattr(runtime, 'open_sessions', {}).values()):
        if w is None or w.worker is None:
            continue
        w.consume()
        if hasattr(w, 'promote_report'):
            w.promote_report(w.worker)
        if w.state.get('turn_complete') or w.state.get('slot_failure') or not w.worker.alive():
            if runtime._reap(w, force=True) and _gate_on(runtime):
                w.state['report_check_due'] = True; w.save()


def _stop_phase(runtime, names, *, hard=False):
    """End sessions of a phase; an in-flight reply is never cut unless hard."""
    waiting = False
    for name in names:
        w = runtime.open_sessions.get(name)
        if w is None or w.worker is None:
            continue
        if _busy(runtime, w) and not hard:
            waiting = True
            continue
        runtime._reap(w, force=True)
    return waiting


# ------------------------------------------------- progress compile (writers)
def _progress_builds(runtime):
    from .tex import build
    pool = getattr(runtime, 'open_build_pool', None)
    if pool is None:
        pool = runtime.open_build_pool = ThreadPoolExecutor(max_workers=2)
    futures = runtime.__dict__.setdefault('open_build_futures', {})
    for name, w in runtime.open_sessions.items():
        if not name.startswith('writer:') or w is None or w.worker is None:
            continue
        future = futures.get(name)
        if future is not None and future.done():
            futures.pop(name)
            try:
                result, private = future.result()
            except Exception:
                continue
            w.state.setdefault('open_builds', []).append(dict(at=runtime.clock(), outcome=result.get('outcome')))
            if result.get('outcome') != 'pass':w.state['last_build_errors'] = str(private or '')[:20000]
            if result.get('outcome') != 'pass' and w.worker is not None:
                w.state['generation'] = w.state.get('generation', 0) + 1
                w.worker.notice(dict(kind='build-failure', generation=w.state['generation'], private_message=private))
            w.save()
            continue
        draft = w.work / 'FINAL.tex'
        if future is None and draft.exists() and draft.stat().st_size > 1000:
            mtime = draft.stat().st_mtime
            if runtime.clock() >= w.state.get('open_build_at', 0) + 180 and mtime > w.state.get('open_build_mtime', 0):
                w.state['open_build_at'] = runtime.clock(); w.state['open_build_mtime'] = mtime
                futures[name] = pool.submit(build, w.work)


# ----------------------------------------------------------------- closing
def _timeline(runtime, state):
    """Phases between handover and freeze, by the configured fractions. A phase whose work is done
    ends early (open_phase_cuts, operator 2026-10-08: do not wait out a finished stage); the later
    phases then share the remaining time in the same proportions."""
    tl = state['closing']['timeline']
    start = tl[0]['end']; end = tl[-1]['start']
    cuts = runtime.__dict__.get('open_phase_cuts') or {}
    fr = list(runtime.config['open_phases'])
    out = []; t = start
    for i, (name, fraction) in enumerate(fr):
        rest = sum(f for _, f in fr[i:])
        nxt = min(end, t + (fraction / rest) * (end - t)) if rest > 0 else end
        if name in cuts and t <= cuts[name] < nxt:
            nxt = cuts[name]
        out.append((name, t, nxt)); t = nxt
    last = out[-1][0]
    if not (last in cuts and out[-1][1] <= cuts[last] < end):
        out[-1] = (last, out[-1][1], end)  # the last phase runs to the freeze unless its work is done
    return out, end


def _writer_done(w):
    """A closing writer whose turn ended and whose report passed the final-report check, ran out of
    retries, or is not checked."""
    if w is None or w.worker is not None or w.state.get('report_check_due') or w.state.get('rewrite_pending'):
        return False
    st = w.state.get('report_check') or {}
    res = st.get('turn_results') or []
    return not st or bool(st.get('exhausted')) or bool(res and res[-1].get('ok'))


def _phase_done(runtime, name):
    """The work of an open closing phase is done: write when the primary writer is done (the backup
    keeps running), critique when every critic session ended, rewrite when every writer is done,
    vote when no repair session runs."""
    sessions = runtime.open_sessions
    order = writers(runtime)
    if name == 'write':
        return 'started:write' in runtime.open_done and bool(order) and _writer_done(sessions.get('writer:' + order[0]))
    if name == 'critique':
        crit = [w for k, w in sessions.items() if k.startswith('critic:')]
        return 'started:critique' in runtime.open_done and all(w is None or w.worker is None for w in crit)
    if name == 'rewrite':
        ws = [sessions.get('writer:' + m) for m in order]
        return 'started:rewrite' in runtime.open_done and all(w is None or _writer_done(w) for w in ws)
    if name == 'vote':
        return 'started:repair' in runtime.open_done and all(w is None or w.worker is None for k, w in sessions.items() if k.startswith('repair:'))
    return False


def _report_bytes(w):
    path = w.work / 'FINAL.tex'
    return path.read_bytes() if path.exists() else b''


def _common_files(runtime):
    src = runtime.watchdog.work
    files = {}
    for name in ('BOARD.json', 'PROBLEM.md', 'SOLUTION-TEMPLATE.tex', 'FINAL-FINDINGS.json'):
        if (src / name).exists():
            files[name] = (src / name).read_bytes()
    art = src / 'board-artifacts'
    if art.is_dir():
        for p in art.iterdir():
            files['board-artifacts/' + p.name] = p.read_bytes()
    return files


def _queue_final_builds(runtime, order):
    """Compile each full member's report as a model bundle, the winner last."""
    q = runtime.__dict__.setdefault('open_final_queue', [])
    for member in order:
        w = runtime.open_sessions.get('writer:' + member)
        if w is not None and (w.work / 'FINAL.tex').exists() and (w.work / 'FINAL.tex').stat().st_size > 0:
            r = runtime.open_sessions.get('repair:' + member)
            repaired = r is not None and (r.work / 'FINAL.tex').exists() and (r.work / 'FINAL.tex').read_bytes() != (w.work / 'FINAL.tex').read_bytes()
            # The repaired source is compiled under the original author's model id.
            path = r.work if repaired else w.work
            if _gate_on(runtime):
                # Final-report check: a failing report is compiled as the fallback (draft kept as FINAL-REJECTED.tex).
                from .report_gate import enforce
                try:w.state.setdefault('report_check', {})['final_outcome'] = enforce(path, w.state)
                except Exception as exc:
                    w.state.setdefault('report_check', {})['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
                    w.save()
                    continue  # never compile a report the check could not clear
                w.save()
            q.append((member, str(path)))


def _drain_final_builds(runtime):
    q = runtime.__dict__.get('open_final_queue') or []
    wd = runtime.watchdog
    if not q or wd.state.get('build_pending'):
        return
    member, path = q.pop(0)
    wd.state.setdefault('open_builds_requested', []).append(dict(member=member, at=runtime.clock()))
    wd.request_build('open-swarm-report:' + member, source=path, model_id=MODEL_IDS[member])


def _tally(runtime):
    full, light = members(runtime)
    order = writers(runtime)
    if order:
        # No vote (role design): primary writer first; builds run reversed, primary last.
        return dict(votes={}, counts={}, verified_counts={}, winner=order[0], order=order,
                    rule='role design: primary writer %s, backup %s' % (order[0], ', '.join(order[1:]) or 'none'))
    votes = {}
    for voter in full + light:
        w = runtime.open_sessions.get('voter:' + voter)
        text = (w.work / 'AUDIT.md').read_text(errors='replace') if w and (w.work / 'AUDIT.md').exists() else ''
        # Lenient (operator ruling 2026-10-06): any casing/markup, e.g. "**Vote:** REPORT-dsf.tex".
        pick = None
        for line in text.splitlines():
            if re.search(r'\bvote\b', line, re.I):
                found = [m for m in full if m != voter and re.search(r'(?<![a-z0-9])' + re.escape(m) + r'(?![a-z0-9])', line.lower())]
                if len(found) == 1:
                    pick = found[0]; break
        if pick:
            votes[voter] = pick
        elif text.strip():
            runtime.watchdog.state.setdefault('open_vote_unread', {})[voter] = text.strip()[:300]
    counts = {m: list(votes.values()).count(m) for m in full}

    def verified(member):
        text = _report_bytes(runtime.open_sessions['writer:' + member]).decode(errors='replace').lower() \
            if 'writer:' + member in runtime.open_sessions else ''
        return len(re.findall(r'(?<!un)verified', text))
    ranked = sorted(full, key=lambda m: (counts[m], verified(m)), reverse=True)
    return dict(votes=votes, counts=counts, verified_counts={m: verified(m) for m in full}, winner=ranked[0], order=ranked)


def drive_closing(runtime, state):
    """Called from SwarmRuntime._drive_closing once the arm is in closing."""
    from .claim_ledger import load_policy
    certification = _SR.writer_certification(load_policy(runtime.strategy))
    runtime.__dict__.setdefault('open_sessions', {})
    runtime.__dict__.setdefault('open_phase', None)
    _drive_sessions(runtime)
    full, light = members(runtime)
    handover = state['closing']['timeline'][0]['end']
    for w in list(runtime.slots.values()):
        if w.stage in ('research', 'research-review'):
            runtime._finish_slot(w, forced=runtime.clock() >= handover)
    if any(s.stage in ('research', 'research-review') for s in runtime.slots.values()):
        return
    if not runtime.author_prepared:
        runtime._snapshot_sources()
        atomic_json(runtime.watchdog.work / 'FINAL-FINDINGS.json', state['findings'])
        runtime.board.enter_report_mode(runtime.secret); runtime.mode = 'report'
    phases, end = _timeline(runtime, state)
    now = runtime.clock()
    current = next((p for p in phases if now < p[2]), None)
    name = current[0] if current else 'done'
    done = runtime.__dict__.setdefault('open_done', [])
    sessions = runtime.open_sessions
    # Phase transitions wait for in-flight replies of the previous phase (hold),
    # but never past the freeze stage.
    prev = runtime.open_phase
    if prev and prev != name and prev not in done:
        names = [k for k in sessions if k.startswith({'write': 'writer:', 'critique': 'critic:', 'rewrite': 'writer:', 'vote': 'voter:'}[prev])]
        # Open check 1: waiting for every in-flight reply let slow writers hold
        # the write phase until freeze, so critique and vote never ran. Idle
        # sessions stop at the boundary; busy ones keep running (never cut) while
        # the next phase starts; anything still running stops at freeze.
        _stop_phase(runtime, names, hard=now >= end)
        done.append(prev)
        if prev == 'critique':
            _distribute_critiques(runtime)
        if prev == 'rewrite':
            pass
    runtime.open_phase = name
    common = _common_files(runtime)
    if name == 'write' and 'started:write' not in done:
        order = writers(runtime)
        for m in order:
            sessions['writer:' + m] = _session(runtime, runtime.assembler + ':open-writer:' + m, m,
                                               WRITE_TASK.format(member=m) + WRITER_RULES + certification + (BACKUP_NOTE.format(primary=order[0]) if m != order[0] else ''),
                                               common) or sessions.get('writer:' + m)
        done.append('started:write')
    if name == 'critique' and 'started:critique' not in done:
        reports = {m: _report_bytes(sessions['writer:' + m]) for m in writers(runtime) if sessions.get('writer:' + m)}
        from .claim_ledger import load_policy
        from .board import MODEL_KEYS
        cw = {str(k).lower(): str(v).lower() for k, v in (load_policy(runtime.strategy).get('critic_weights') or {}).items()}
        for c in full + light:
            if cw.get(str(MODEL_KEYS.get(c, c)).lower()) == 'none':
                continue  # operator 2026-10-08: no closing critique by a model that is not a critic (it returned nothing usable)
            files = dict(common)
            for m, data in reports.items():
                if m != c and data:
                    files['REPORT-%s.tex' % m] = data
            sessions['critic:' + c] = _session(runtime, runtime.assembler + ':open-critic:' + c, c,
                                               CRITIQUE_TASK.format(member=c), files)
        done.append('started:critique')
    if name == 'rewrite' and 'started:rewrite' not in done:
        for m in writers(runtime):
            w = sessions.get('writer:' + m)
            if w is not None and w.worker is None:
                _restart(runtime, w, REWRITE_TASK.format(member=m) + WRITER_RULES + certification, m)
            elif w is not None:
                w.state['rewrite_pending'] = True; w.save()  # still drafting; revise when it finishes
        done.append('started:rewrite')
    if name == 'vote' and 'started:repair' not in done:
        # Operator ~23:40: a DeepSeek Flash session repairs LaTeX of any report whose
        # latest compile failed; the report stays its author's.
        for m in dict.fromkeys(full + writers(runtime)):
            w = sessions.get('writer:' + m)
            if w is None or not (w.work / 'FINAL.tex').exists():
                continue
            builds = w.state.get('open_builds') or []
            if builds and builds[-1].get('outcome') == 'pass':
                continue
            files = {p.name: p.read_bytes() for p in w.work.iterdir() if p.is_file() and p.suffix in ('.tex', '.bib', '.sty', '.cls', '.bbl')}
            files['LATEX-ERRORS.txt'] = (w.state.get('last_build_errors') or 'Compile failed; run the build to see errors.').encode()
            r = _session(runtime, runtime.assembler + ':open-repair:' + m, 'dsf', REPAIR_TASK, files)
            if r is not None:
                sessions['repair:' + m] = r
        done.append('started:repair')
    if name == 'vote' and 'started:vote' not in done and False:  # no vote (role design)
        reports = {m: _report_bytes(sessions['writer:' + m]) for m in full if sessions.get('writer:' + m)}
        for v in full + light:
            files = {'PROBLEM.md': common.get('PROBLEM.md', b'')}
            files.update({'REPORT-%s.tex' % m: d for m, d in reports.items() if d})
            sessions['voter:' + v] = _session(runtime, runtime.assembler + ':open-voter:' + v, v,
                                              VOTE_TASK.format(member=v), files)
        done.append('started:vote')
    _report_retries(runtime, sessions, name, current, now)
    if name == 'vote':
        # Critiques are read only in rewrite: a critic still running when the vote phase starts is stopped.
        _stop_phase(runtime, [k for k in sessions if k.startswith('critic:')], hard=True)
    if current and name in ('write', 'critique', 'rewrite', 'vote') and name not in runtime.__dict__.setdefault('open_phase_cuts', {}) \
            and _phase_done(runtime, name):
        runtime.open_phase_cuts[name] = now  # the next phase starts at the next step
    if name in ('rewrite', 'vote'):
        # Late critics (still running at the boundary) deliver when they finish.
        _distribute_critiques(runtime)
    if name == 'rewrite':
        for m in writers(runtime):
            w = sessions.get('writer:' + m)
            if w is not None and w.worker is None and w.state.pop('rewrite_pending', False):
                _restart(runtime, w, REWRITE_TASK.format(member=m) + WRITER_RULES + certification, m)
    if name in ('write', 'critique', 'rewrite', 'vote'):
        _progress_builds(runtime)
    if name == 'done' and 'tallied' not in done:
        result = _tally(runtime)
        runtime.watchdog.state['open_vote'] = result; runtime.watchdog.save()
        runtime.board.publish_control_record(runtime.secret, identity='open-vote', kind='final-findings', body=result)
        _queue_final_builds(runtime, list(reversed(result['order'])))  # winner compiled last
        done.append('tallied')
    _drain_final_builds(runtime)
    if name == 'done' and 'tallied' in done and not runtime.__dict__.get('open_final_queue') \
            and not runtime.watchdog.state.get('build_pending'):
        # Every report is built: the remaining executor stages hold nothing (do not wait them out).
        st = runtime.executor.snapshot()
        if st['mode'] == 'closing' and st['closing']['stage'] != 'freeze':
            try:
                runtime.executor.finish_stage(st['closing']['stage'])
            except ValueError:
                pass


CHECK_TASK = ('You are {member}. Revise YOUR report FINAL.tex so that it passes the harness final-report check; '
              'the failure list follows. Change only what the list names. ')


def _gate_on(runtime):
    return runtime._report_gate_on() if hasattr(runtime, '_report_gate_on') else False


def _report_retries(runtime, sessions, phase, current, now):
    """Final-report check after a writer turn (write/rewrite phases): bounded relaunch with the
    generic failure list while the phase has enough time left."""
    if not _gate_on(runtime):
        return
    from .report_gate import turn_end, settings, retry_note
    from .claim_ledger import load_policy
    limits = settings(runtime.strategy)
    for m in writers(runtime):
        w = sessions.get('writer:' + m)
        if w is None or w.worker is not None or not w.state.pop('report_check_due', False):
            continue
        left = (current[2] - now) if current and phase in ('write', 'rewrite') else 0
        try:
            r = turn_end(w.state, w.work, time_left=left, limits=limits)
        except Exception as exc:
            w.state.setdefault('report_check', {})['error'] = type(exc).__name__ + ': ' + str(exc)[:200]; w.save()
            continue
        w.save()
        if r == 'retry':
            _restart(runtime, w, CHECK_TASK.format(member=m) + WRITER_RULES + _SR.writer_certification(load_policy(runtime.strategy)) + retry_note(w.state), m)
            w.save()


def _distribute_critiques(runtime):
    full, light = members(runtime)
    sessions = runtime.open_sessions
    full = list(dict.fromkeys(full + writers(runtime)))
    received = {m: [] for m in full}
    for c in full + light:
        w = sessions.get('critic:' + c)
        text = (w.work / 'AUDIT.md').read_text(errors='replace') if w and (w.work / 'AUDIT.md').exists() else ''
        if not text:
            continue
        parts = re.split(r'(?m)^##\s*On\s+', text)
        matched = False
        for part in parts[1:]:
            target = part.split(None, 1)[0].strip().lower().strip(':')
            if target in received and target != c:
                received[target].append('### From %s\n%s' % (c, part.split('\n', 1)[1] if '\n' in part else part)); matched = True
        if not matched:
            for m in full:
                if m != c:
                    received[m].append('### From %s (unsectioned)\n%s' % (c, text))
    for m in full:
        w = sessions.get('writer:' + m)
        if w is not None:
            (w.work / 'CRITIQUES-RECEIVED.md').write_text('\n\n'.join(received[m]) or 'No critiques were received.')
            w.state['critiques_received'] = len(received[m]); w.save()


def stop_all(runtime):
    for w in list(getattr(runtime, 'open_sessions', {}).values()):
        if w is not None and w.worker is not None:
            w.state['slot_closed'] = True
            try:
                w.consume()
            except Exception:
                pass
            w.stop_worker(); w.save()


def finalize(runtime):
    """At freeze: tally the vote (if the vote phase was cut short, with whatever
    votes exist), then compile every full member's report, the winner last.
    Returns True once all builds are done."""
    if not getattr(runtime, 'open_sessions', None):
        return True
    done = runtime.__dict__.setdefault('open_done', [])
    if 'tallied' not in done:
        result = _tally(runtime)
        runtime.watchdog.state['open_vote'] = result
        runtime.watchdog.state['open_member_stats'] = member_stats(runtime); runtime.watchdog.save()
        try:
            runtime.board.publish_control_record(runtime.secret, identity='open-vote', kind='final-findings', body=result)
        except Exception:
            pass
        _queue_final_builds(runtime, list(reversed(result['order'])))
        done.append('tallied')
    runtime.watchdog.closure_poll()
    _drain_final_builds(runtime)
    finished = not runtime.__dict__.get('open_final_queue') and not runtime.watchdog.state.get('build_pending')
    if finished and 'selected' not in done:
        # Highest-voted COMPILED report (coordinator): builds run in reverse vote
        # order, so the last compiled bundle is the best-ranked one that compiled.
        wd = runtime.watchdog; order = wd.state.get('open_vote', {}).get('order', [])
        compiled = set()
        for row in wd.state.get('build_history', []):
            pass
        built = [r['member'] for r in wd.state.get('open_builds_requested', [])]
        bundles = list((wd.state.get('closure_bundles') or {}).values())
        wd.state['open_selected'] = dict(order=order, requested=built, compiled_bundles=len(bundles),
            rule='highest-voted report that compiled (ties: most verified claims)')
        wd.save(); done.append('selected')
    return finished


def member_stats(runtime):
    """Per-member board activity for the freeze report (coordinator)."""
    import collections
    stats = collections.defaultdict(lambda: collections.Counter())
    try:
        rows = runtime.board.export_snapshot(runtime.secret)
        records = json.loads(rows['snapshot']).get('records', []) if isinstance(rows, dict) and 'snapshot' in rows else []
    except Exception:
        records = []
    for r in records:
        author = str(r.get('author', '?'))
        member = next((m for m in MODEL_IDS if (':%s:' % m) in author or author.endswith(':' + m) or ('open:%s' % m) in author), author[-24:])
        kind = r.get('kind')
        stats[member][kind] += 1
        if kind == 'finding':
            stats[member]['finding:' + str((r.get('body') or {}).get('status'))] += 1
    reads = runtime.__dict__.get('open_note_stats', {})
    return dict(posts={m: dict(c) for m, c in stats.items()}, note_deliveries=reads)

# The same stored claim-type separation applies to every final writer.
from .swarm_runtime import WRITER_LITERATURE
WRITE_TASK += WRITER_LITERATURE
REWRITE_TASK += WRITER_LITERATURE
