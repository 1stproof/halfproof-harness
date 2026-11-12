import os
import tempfile
import hashlib
import json
import re
from pathlib import Path

START = 1792030380  # 2026-10-14 19:13 PDT
GATE = START + 4*3600
STOP = START + 6*3600
WHOLE_REQUEST_SECONDS = 3600
ROUTES = {'dsf': ('deepseek/deepseek-v4.1-flash', 'together'),
          'qwen24t': ('qwen/qwen3.8-2.4t-a95b', 'alibaba'),
          'k3': ('moonshotai/kimi-k3', 'moonshotai/mxfp4'),
          'dsp': ('deepseek/deepseek-v4-pro', 'alibaba')}

def validate_routes(authority,roster):
    verified={(r['model'],r['provider']) for r in authority.get('routes',[])
              if r.get('contract_route_allowed') is True}
    if not authority.get('routes_verified') or not set(ROUTES)<=set(roster) or not set(ROUTES.values())<=verified:
        raise ValueError('a configured model/provider route lacks verified contract authority')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def save(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True).encode())


def parse(raw):
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n',1)[1].rsplit('```',1)[0]
    return json.loads(raw)

def inventory(manifest):
    rows = [c for c in manifest['claims'] if c.get('certified') is True]
    ids = [c['current_record'] for c in rows]
    if len(ids) != len(set(ids)) or any(not c.get('text') or not c.get('statement') for c in rows):
        raise ValueError('invalid certified inventory')
    return rows

def validate_plan(plan, manifest):
    known = {c['current_record'] for c in inventory(manifest)}
    main = plan['main_records']
    included = plan['included_records']
    if not main or len(included)!=len(set(included)) or not set(main)<=set(included)<=known:
        raise ValueError('main/closure records must be certified and unique')
    edges = plan['dependencies']
    reached = set()
    def visit(n):
        if n in reached: return
        reached.add(n)
        if str(n) not in edges: raise ValueError('missing dependency inventory')
        for dep in edges[str(n)]:
            if dep not in included: raise ValueError('dependency outside closure')
            visit(dep)
    for n in main: visit(n)
    if reached != set(included): raise ValueError('extraneous proved record outside main closure')
    if not isinstance(plan.get('source_dependency_dispositions'),list):
        raise ValueError('original premise dispositions absent')
    # Every original premise is either a graph edge or explicitly accounted for
    # by the roster (folded correction, external citation, or unresolved).
    dispositions = {(d['record'],d['premise']): d for d in plan['source_dependency_dispositions']}
    for c in inventory(manifest):
        n=c['current_record']
        if n not in included: continue
        for p in c.get('premise_refs',[]):
            if p in edges[str(n)]: continue
            d=dispositions.get((n,p))
            if not d or d.get('kind') not in ('folded','citation','not_used','unresolved') or not d.get('reason'):
                raise ValueError('unaccounted original premise '+str((n,p)))
    return plan

def request_allowed(now, stop=STOP, reserve=WHOLE_REQUEST_SECONDS):
    return now + reserve <= stop

RULES = '''You are a member of this run's original open-weight roster. This is a
writing-only continuation based on its certified records and original problem.
All mathematical choices are yours. Choose the main result(s) from that evidence
and problem; prove exactly those results and their necessary dependency closure.
Omit unrelated certified results (at most a one-line remark). Fold retractions,
repairs and corrections into final statements, not separate theorems. Preserve
qualifications. An uncertified assertion or incomplete proof must be omitted or
put in a short explicitly labelled open-points section, never asserted as proved.
A specific published or standard result may be cited instead of reproved; name it
precisely and verify its hypotheses. Bare 'classical' is incomplete. No external
judge feedback is available or permitted. Write from the certified arguments,
not merely their conclusions. Do not include ledger narration or audit history.
'''
PLAN = RULES + '''Plan an ordinary 10-30 page mathematics paper before drafting.
Return JSON only with main_records:[integer], included_records:[integer],
dependencies:{"record":[integer dependencies]}, source_dependency_dispositions:
[{record:integer,premise:integer,kind:"folded|citation|not_used|unresolved",reason:string}],
title:string, outline:[{heading:string,records:[integer],purpose:string,pages:number}],
notation:string, synthesis:string, open_points:string.
The included_records must be exactly the transitive closure of main_records
under dependencies. Account for every original premise_ref either as an edge or
in source_dependency_dispositions. Explain folding/citations in your own words.
Do not optimize any external score. Choose substantive main results that respond
to the original problem, and identify honestly what remains open. Plan coherent
sections, not a catalogue with one section per record. Allocate space to proofs.
'''
DRAFT = RULES + r'''Write the entire planned mathematics paper as a standalone LaTeX
article, 12pt and ordinary margins, targeting 10-30 pages. Return ONLY the complete
.tex file, no commentary. Include title, abstract, introduction explaining main
results and their relation to the problem, notation/preliminaries, logically
ordered lemmas/theorems each followed by a complete proof, and references using
an inline thebibliography environment. Include short open points if necessary.
Use the plan to limit scope. Avoid repeated proofs or repeating the same result
as multiple numbered statements. Put a unique \label{...} on every proved
statement (including cited/conjectural statements) and immediately before it a comment % RECORDS: n,m identifying its
certified source records. For cited or conjectural environments use instead an explicit comment % STATUS: cited or % STATUS: conjectural immediately above the environment. The main results and their necessary lemmas alone may
be stated as proved. Every dependency must be proved in the paper or specifically
cited with its hypotheses checked. Explain notation consistently. Use standard
LaTeX packages; no external files, shell commands or bibliography tools.
Do not pad a short paper to reach the page target or compress away proof steps.
'''
REFEREE = RULES + '''Act as the roster's internal referee and editor. Read the
whole candidate against the certified arguments and plan. Check completeness,
certified provenance, hypotheses, dependency closure, corrections folded in,
coherent mathematical paper structure, and length. Return JSON only:
{accept:boolean,paper_like:boolean,feedback:string,main_records:[integer],
proved_records:[integer],dependencies_complete:boolean,
statements:[{label:string,source_records:[integer],proof_complete:boolean,
source_certified:boolean,needed_for_main:boolean,dependencies_complete:boolean,reason:string}]}.
Certification is not a substitute for checking the proof actually written.
Independently inventory every result and substantive prose assertion presented
as proved, then audit each one in statements. Do not accept merely because its
source is called certified. Check every substantive inference, including cited
results and their hypotheses. All statements must pass the per-statement audit
before accept=true. Your feedback is for your own roster, not an external judge.
Accept only a complete coherent manuscript whose proved claims have complete
certified arguments and dependencies. If not acceptable give your own detailed
feedback for the roster author. This is an internal review, not external judging.
'''
SELECT = RULES + '''You are the roster's final editor. Select explicitly among the
supplied complete candidates using their text, own referee reports and compilation
results. Do not select merely because a file is newest. Prefer a coherent paper
with complete certified proofs. Return JSON {candidate:integer,reason:string,
paper_like:boolean}. Choose only an available compiled candidate if any compiles.
If none compiles choose the most suitable manuscript and explain that limitation.
'''
