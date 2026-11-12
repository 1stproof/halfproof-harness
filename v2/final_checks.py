"""Exact-byte final verification contract, shared by swarms and solos.

Extraction is deterministic and deliberately not a correctness judgment. Every
verifier must report uncatalogued claims; absence, retrieval failure and mismatched
attribution are findings. A URL written by a model is never a retrieval receipt.
"""
import hashlib
import json
import re
from .contracts_v3 import canonical_json

FAMILIES = {'dsf':'DeepSeek','dsp':'DeepSeek','qwen24t':'Qwen','k3':'Kimi','glm':'GLM'}
CITATION_STATUS = {'supported','mismatched','unavailable','unchecked'}
CLAIM_STATUS = {'supported','defect','unknown','unchecked'}

FINAL_CHECK_PROMPT = '''Independently check the exact candidate and original problem.
Return candidate_sha256, source_sha256, rendered_sha256, citation_ledger,
claim_ledger, uncatalogued_claims, inventory_discrepancies. Cover every supplied
inventory ID and every additional reference, attribution and claimed result you
find. For each citation give id, bibliographic_identity, attributed, status
(supported/mismatched/unavailable/unchecked), receipt_id, source_sha256 and
location. Retrieve original sources: arXiv LaTeX source first, HTML fallback.
Inspect the source_index omissions and request additional location/start/limit
slices when needed; a default excerpt is not the complete source.
A URL, search snippet, prior worker assertion or bibliographic existence alone
cannot support an attribution. For each claim give id, assumptions, dependencies,
checked_steps and status (supported/defect/unknown/unchecked). Explain defects
and uncertainty. Do not edit the candidate, recruit workers or begin research.
Do not infer truth from orchestrator approval or peer agreement. Exhausted time,
missing source and missing coverage are unresolved, never approval.
PACKET.json includes the controller's current claim_manifest. Independently compare
EVERY assertion (including prose, abstract and summary) and its proof with the full
current statement/proof. Check hypotheses, quantifiers, finite/infinite, signs,
inequalities, source setting, and proof steps. A shared title or record ID supplies
no support. Each claimed result must report support_bindings: exact current claim_id,
text_sha256, evidence_sha256, policy_sha256, and checked_steps. Faithful restatements
are allowed only after this semantic comparison; new inferences need independent
reasoning review. Literature is source evidence, never a proved applicability claim.'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(files, *, rendered=b'', claim_manifest=None):
    """Inventory each bibliography entry, citation occurrence and theorem result.

    Source bundle hashing uses the same dependency-manifest recipe as P6.
    Plain-prose results can escape regex extraction; verifier declarations are
    therefore mandatory and extraction coverage is explicitly qualified.
    """
    manifest={name:digest(data) for name,data in sorted(files.items())}
    source=digest(canonical_json(manifest))
    citations=[];claims=[]
    for name,data in sorted(files.items()):
        if not name.endswith(('.tex','.bib')):continue
        text=data.decode('utf-8',errors='replace')
        patterns=[('reference',r'\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}'),
                  ('reference',r'@\w+\s*\{\s*([^,]+),'),
                  ('attribution',r'\\cite\w*\*?(?:\[[^\]]*\])*\{([^}]+)\}')]
        for kind,pattern in patterns:
            for m in re.finditer(pattern,text):
                for key in m.group(1).split(','):
                    citations.append(dict(id=f'{name}:{m.start()}:{kind}:{key.strip()}',kind=kind,
                        reference=key.strip(),location=f'{name}:{text[:m.start()].count(chr(10))+1}',
                        attributed=text[max(0,m.start()-200):m.end()+400]))
        # Explicit prose attributions are separate from nearby citation macros.
        # Verifiers still identify other natural-language attribution patterns.
        for m in re.finditer(r'(?i)(?:according\s+to|as\s+(?:shown|proved|established|stated)\s+by)\s+([^.!?\n]{1,300})[.!?]',text):
            citations.append(dict(id=f'{name}:{m.start()}:prose-attribution',kind='prose-attribution',
                reference=m.group(1).strip(),location=f'{name}:{text[:m.start()].count(chr(10))+1}',attributed=m.group(0)))
        for m in re.finditer(r'\\begin\{(theorem|lemma|proposition|corollary|claim|conjecture)\}(.*?)(?:\\end\{\1\})',text,re.S):
            claims.append(dict(id=f'{name}:{m.start()}:claim',kind=m.group(1),statement=m.group(2),
                location=f'{name}:{text[:m.start()].count(chr(10))+1}'))
    result = dict(candidate_sha256=source,source_sha256=source,rendered_sha256=digest(rendered),
        citations=citations,claims=claims,document_present=bool(files.get('FINAL.tex')),rendered_present=bool(rendered),extraction='deterministic-partial; verifier must identify uncatalogued claims')
    if claim_manifest is not None:
        result['claim_manifest'] = claim_manifest
    return result


def reviewers_valid(writer_id, reviewers, *, mixed):
    if len(reviewers)!=2 or len({r['worker'] for r in reviewers})!=2:
        raise ValueError('two distinct nonwriter reviewers required')
    if any(r['worker']==writer_id or r['model'] not in FAMILIES for r in reviewers):
        raise ValueError('invalid reviewer identity')
    if mixed and len({FAMILIES[r['model']] for r in reviewers})!=2:
        raise ValueError('mixed reviewers require two different families')
    if not mixed and any(r['model']!='dsf' for r in reviewers):
        raise ValueError('DSF-only final review')
    return True


def evaluate(report, inv, *, receipts, source_bytes):
    """Validate authenticated session output against controller-owned receipts.

    source_bytes maps hashes to actual retained source bytes. Caller authenticates
    reviewer identity outside the model JSON. No model-authored pass flag is used.
    """
    findings=[]
    if not isinstance(report,dict):
        findings.append(dict(kind='malformed-review'));report={}
    # Lenient on format only (operator ruling 2026-10-06). Missing attestation stays
    # unsupported: hashes and ledgers are never filled in (review 903b791 #3).
    report=dict(report)
    for field in ('candidate_sha256','source_sha256','rendered_sha256'):
        if isinstance(report.get(field),str):report[field]=report[field].strip().lower()
    for field in ('uncatalogued_claims','inventory_discrepancies','citation_ledger','claim_ledger'):
        if isinstance(report.get(field),dict):report[field]=[dict(v,id=v.get('id',k)) if isinstance(v,dict) else v for k,v in report[field].items()]
    for field in ('citation_ledger','claim_ledger'):
        for row in report[field] if isinstance(report.get(field),list) else []:
            if isinstance(row,dict):
                if 'id' in row and isinstance(row['id'],(int,float)):row['id']=str(row['id'])
                if isinstance(row.get('status'),str):row['status']=row['status'].strip().lower().replace(' ','-').replace('_','-')
    if not inv.get('document_present'):findings.append(dict(kind='missing-document'))
    if not inv.get('rendered_present'):findings.append(dict(kind='unbuilt-document'))
    for field in ('candidate_sha256','source_sha256','rendered_sha256'):
        if report.get(field)!=inv[field]:findings.append(dict(kind='hash-mismatch',field=field))
    for field in ('uncatalogued_claims','inventory_discrepancies'):
        if not isinstance(report.get(field),list):findings.append(dict(kind='missing-'+field))
        elif report[field]:findings.append(dict(kind=field,items=report[field]))
    for kind,field,statuses in [('citation','citation_ledger',CITATION_STATUS),('claim','claim_ledger',CLAIM_STATUS)]:
        rows=report.get(field)
        if not isinstance(rows,list):
            findings.append(dict(kind='missing-'+field));rows=[]
        indexed={}
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str):
                findings.append(dict(kind='malformed-'+kind));continue
            if row['id'] in indexed:findings.append(dict(kind='duplicate-'+kind,id=row['id']))
            indexed[row['id']]=row
            if not isinstance(row.get('status'),str) or row['status'] not in statuses:findings.append(dict(kind='invalid-disposition',id=row['id']))
            elif row['status']!='supported':findings.append(dict(kind=kind,id=row['id'],status=row['status'],detail=row))
            if kind=='claim':
                if any(not isinstance(row.get(k),list) for k in ('assumptions','dependencies','checked_steps')) or (row.get('status')=='supported' and not row.get('checked_steps')):
                    findings.append(dict(kind='incomplete-claim',id=row['id']))
            else:
                if any(not isinstance(row.get(k),str) or not row[k].strip() for k in ('bibliographic_identity','attributed')):
                    findings.append(dict(kind='incomplete-citation',id=row['id']))
                if row.get('status')=='supported':
                    receipt_id=row.get('receipt_id');receipt=receipts.get(receipt_id) if isinstance(receipt_id,str) else None
                    hashed=row.get('source_sha256');data=source_bytes.get(hashed) if isinstance(hashed,str) else None
                    if (not receipt or receipt.get('sha256')!=hashed or data is None
                            or digest(data)!=hashed or not row.get('location')):
                        findings.append(dict(kind='unauthenticated-retrieval',id=row['id']))
        for row in inv['citations' if kind=='citation' else 'claims']:
            if row['id'] not in indexed:findings.append(dict(kind='missing-'+kind,id=row['id']))
    if 'claim_manifest' in inv:
        current = {c['claim_id']: c for c in inv['claim_manifest']['claims']}
        # Every result, including plain prose catalogued by the verifier, needs a
        # current exact evidence binding. The final auditor judges entailment.
        for row in report.get('claim_ledger', []) if isinstance(report.get('claim_ledger'), list) else []:
            if not isinstance(row, dict): continue
            bindings = row.get('support_bindings')
            if not isinstance(bindings, list) or not bindings:
                findings.append(dict(kind='missing-support-binding', id=row.get('id')))
                continue
            for binding in bindings:
                c = current.get(binding.get('claim_id')) if isinstance(binding, dict) else None
                if (not c or c['state'] != 'accepted' or c.get('post_type') == 'literature' or
                        any(binding.get(k) != c[k] for k in ('text_sha256','evidence_sha256','policy_sha256')) or
                        not isinstance(binding.get('checked_steps'), list) or not binding['checked_steps']):
                    findings.append(dict(kind='invalid-support-binding', id=row.get('id')))
    return dict(status='internal-supported' if not findings else 'verification_incomplete',
                findings=findings,**{k:inv[k] for k in ('candidate_sha256','source_sha256','rendered_sha256')})


def invalidate(previous, current):
    """Any changed source or rendered bytes require a fresh hash-bound check."""
    return any(previous.get(k)!=current.get(k) for k in ('candidate_sha256','source_sha256','rendered_sha256','claim_manifest'))


def check_summary(state, *, supported=False):
    """Administrative measurement, never approval or proof of transport absence.

    Controller check packets bind revisions. Keep adverse findings from all checks
    of these exact bytes/authority when the final quorum is unresolved. A missing
    or malformed review is absence of usable evidence, not a provider receipt.
    """
    candidate = state.get('candidate') or {}
    fields = ('candidate_sha256', 'source_sha256', 'rendered_sha256', 'claim_manifest')
    summary = dict(candidate=candidate, missing_reviews=[], adverse_findings=[],
                   transport_absence_proven=False)
    if supported:
        return summary
    for stage, check in state.get('checks', {}).items():
        captured = check.get('candidate') or {}
        if not candidate or any(captured.get(k) != candidate.get(k) for k in fields):
            continue
        reports = check.get('reports', {})
        for reviewer in check.get('reviewers', []):
            worker = reviewer.get('worker') if isinstance(reviewer, dict) else reviewer
            report = reports.get(worker)
            findings = report.get('findings', []) if report else []
            missing = not report or any(f.get('kind') == 'missing-or-malformed-review' for f in findings)
            if missing:
                summary['missing_reviews'].append(dict(stage=stage, worker=worker,
                    reason='absent-or-malformed; transport cause unproven'))
            adverse = [f for f in findings if f.get('kind') != 'missing-or-malformed-review']
            if report and report.get('status') != 'internal-supported' and not findings:
                adverse = [dict(kind='unresolved-review-disposition')]
            summary['adverse_findings'].extend(dict(stage=stage, worker=worker, finding=f) for f in adverse)
    return summary
