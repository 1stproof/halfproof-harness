"""Notice wire format and atomic state storage; no model or network calls.
The probe hook fetches nonce-bearing output from host memory; Qwen may record it.
"""
import json
import os
from pathlib import Path
import tempfile

NOTICES = {
    'consolidate': 'CONSOLIDATE DURABLE NOTES FIRST. The harness has copied NOTES.md and EXPERIMENTS.md verbatim into the newest numbered notes-archive/ directory. You may read it. Rewrite BOTH live files concisely: current status, results with archive pointers, open obligations and next step. Preserve important information by reference; you decide what stays. Aim for the pair below 78,643 bytes combined (20% of the pinned 98,304-token compaction trigger, bytes / 4 estimated tokens), within fifteen minutes. Then resume the task or pending write-up. Never rewrite the archive.',
    'consolidate-repeat': 'ONE REPEAT: consolidate NOTES.md and EXPERIMENTS.md into concise current status, results with pointers into the newest notes-archive/ directory, open obligations and next step. The verbatim archive remains readable. Get the live pair below 78,643 bytes combined (20% of the pinned 98,304-token compaction trigger, bytes / 4). Then continue the task or pending write-up. No pause will follow non-compliance.',
    'checkpoint': 'Save a concise NOTES.md or EXPERIMENTS.md checkpoint now: established results, unresolved issues, and the next step. Then continue.',
    'checkpoint-final': 'Final checkpoint request: save NOTES.md or EXPERIMENTS.md now. A no-write restart follows after five minutes at a response boundary, with a fifteen-minute absolute grace bound.',
    'write-up-targeted': 'WRITE-UP: FINAL.tex is overdue or AUDIT.md is missing. Write a minimal document containing \\begin{document} now; expand it incrementally, then write AUDIT.md. State gaps honestly. Choose any disk files you need to read.',
    'write-up-fresh': 'WRITE-UP FIRST: Write FINAL.tex first, built incrementally with \\begin{document}, then AUDIT.md. Use the files as they stand, state gaps explicitly. Choose what to read, including your notes. Start no new lines of attack.',
    'write-up-minimal': 'MINIMAL REPORT: write FINAL.tex now from the files as they stand, containing \\begin{document}. Build it incrementally, explicitly label unproved claims and gaps, then write AUDIT.md. You choose which files to read, including notes. Start no new research.',
    'write-up': 'WRITE-UP PHASE. Finish FINAL.tex and AUDIT.md; state every gap explicitly. Update NOTES.md and BLOCKED.md. Start no new lines of attack.',
    'session-wall': 'This session ends in at most five minutes. Save a disk checkpoint now so a fresh session can recover the work.',
}

try: from .contracts import WRITEUP, AUDIT, BUILD
except ImportError: from contracts import WRITEUP, AUDIT, BUILD
for _kind in ('write-up','write-up-targeted','write-up-fresh','write-up-minimal'):
    NOTICES[_kind] = ('WRITE-UP PHASE' if _kind=='write-up' else _kind.upper())+': '+WRITEUP+' '+BUILD
NOTICES['write-up-rejected'] = ('WRITE-UP-REJECTED: Verification rejected your claimed result twice. There will be no further verification '
    'and no new line of attack. The latest verifier findings are in VERIFICATION.json. Rewrite FINAL.tex so that it claims only what '
    'survives. Near the start, add a section that explains the error the verifier found and analyses it: the exact step that fails and '
    'why, which results survive and which depend on the failed step, and whether and how the gap might be repaired. Then update AUDIT.md '
    'and BLOCKED.md, and declare the goal complete. '+WRITEUP+' '+BUILD)
NOTICES['audit-polish'] = AUDIT + ' Write-up remains sticky: only check claims already in the report; preserve new research for follow-up. ' + BUILD
CONTINUE_RESEARCH = 'Research time remains: about {m} minutes before the scheduled research stop. Keep working on the problem; do not end early. (Early-end request {n} of 3 refused; a fourth request will be honored.)'
NOTICES['build-failure'] = 'The harness build failed. Repair the reported errors and compile again. ' + BUILD
NOTICES['checkpoint'] = 'No qualifying checkpoint was observed; inspect existing artifacts and preserve any missing result. ' + NOTICES['checkpoint']


def atomic_json(path, value):
    """Single-writer use only. fsync data, replace, fsync parent directory."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def hook_output(notice, event, nonce):
    """No tool inputs or outputs are returned/logged. Repeated delivery is safe.
    Receipt/emission is NOT confirmation that the model has consumed this context.
    """
    if len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise ValueError('invalid nonce')
    if event not in ('PreToolUse', 'PostToolUse'):
        return {}
    if set(notice) != {'generation', 'kind'}:
        raise ValueError('invalid notice fields')
    generation = notice['generation']
    if type(generation) is not int or generation < 1:
        raise ValueError('invalid generation')
    kind = notice['kind']
    if kind.startswith('continue-research-'):
        minutes, count = kind[len('continue-research-'):].split('-')
        text = CONTINUE_RESEARCH.format(m=int(minutes), n=int(count))
    else:
        text = NOTICES[kind]
    return {'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': f'[harness-notice:{nonce}:{generation}] {text}'}}
