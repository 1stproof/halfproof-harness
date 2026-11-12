"""P2 adapter for P6 closure operations in P7's single isolated publisher.

No background process/thread is created here. The controller submits immutable
operation identities and polls; all compiler/catalog/durable I/O is in perform.
Payloads and diagnostics are private service traffic, not public telemetry.
"""
import hashlib
from pathlib import Path
from .contracts_v3 import canonical_json


def candidate_hash(root):
    from .closure_build import report_dependencies
    files = report_dependencies(root)
    return hashlib.sha256(canonical_json({k:hashlib.sha256(v).hexdigest()
                                         for k,v in sorted(files.items())})).hexdigest()


def perform(operation, payload):
    """Only P7's already isolated publisher may invoke this function."""
    if operation.startswith('cohort-'):
        from .cohort_publication import perform as cohort_perform
        return cohort_perform(operation, payload)
    from .closure import ClosureCatalog
    from .closure_build import ReportCompiler
    catalog = ClosureCatalog(**payload['catalog'])
    if operation == 'preflight':
        record = catalog.preflight(ReportCompiler(payload['scratch']))
        return {'fallback_bundle':record['bundle_id']}
    if operation == 'retain-draft':
        import base64
        files={k:base64.b64decode(v,validate=True) for k,v in payload['files'].items()}
        record=catalog.retain_draft(files,author_id=payload['author_id'],model_id=payload['model_id'],
                                    captured_at_ns=payload['captured_at_ns'],checks=payload.get('checks'),copy=payload.get('copy',False))
        return {'draft_bundle_id': record['bundle_id'] if record else None}
    if operation == 'build':
        result = ReportCompiler(payload['scratch']).compile(payload['source'])
        if payload.get('writeup_time_ns') is not None:result.metadata['writeup_time_ns']=payload['writeup_time_ns']
        response = dict(metadata=result.metadata, diagnostics=result.diagnostics,
                        candidate_hash=result.candidate_sha256)
        if result.passed:
            if payload.get('report_gate_fallback'):result.metadata['report_gate_fallback']=True
            record = catalog.publish_model(result, author_id=payload['author_id'],
                                            model_id=payload['model_id'],captured_at_ns=payload.get('captured_at_ns'),copy=payload.get('copy',False))
            response['bundle_id'] = record['bundle_id']
        elif not payload.get('report_gate_fallback'):
            # Dependency rejection can leave compile.files empty. Keep only the
            # bounded no-follow source in that case, never a directory sweep.
            if not result.files.get('FINAL.tex'):
                from .closure_io import read_file, ClosureError
                try: result.files['FINAL.tex'] = read_file(payload['source'], 'FINAL.tex')
                except (OSError, ClosureError): pass
            if result.files.get('FINAL.tex'):
                record = catalog.retain_failed_source(result, author_id=payload['author_id'], model_id=payload['model_id'],captured_at_ns=payload.get('captured_at_ns'),copy=payload.get('copy',False))
                response['degraded_bundle_id'] = record['bundle_id']
        return response
    if operation == 'status':
        if payload.get('bundle_id') is None and payload['status'] == 'internal-rejected':
            catalog.record_candidate_rejection(payload['candidate_hash'],
                authenticated_record_id=payload['record_id'])
            return {'candidate_hash':payload['candidate_hash'], 'status':'internal-rejected'}
        return catalog.record_status(payload['bundle_id'], candidate_sha256=payload['candidate_hash'],
            status=payload['status'], authenticated_record_id=payload['record_id'])
    if operation == 'select-final':
        return catalog.select_final()
    if operation == 'close':
        if payload.get('draft_sources') and catalog._load()['terminal'] is None:
            from .deadline_writeup import capture
            capture(catalog,payload['draft_sources'])
        return catalog.close(stop_reason=payload['reason'], stopped_at=payload['stopped_at'],
                             operational_cause=payload.get('operational_cause'),
                             reviewed_bundle=payload.get('reviewed_bundle'),
                             verification_status=payload.get('verification_status'),
                             required_claim_manifest_sha256=payload.get('required_claim_manifest_sha256'),
                             check_summary=payload.get('check_summary'))
    raise ValueError('unknown closure operation')


class RuntimeClosure:
    def __init__(self, publisher, state, *, catalog, scratch):
        self.publisher, self.state = publisher, state
        self.catalog, self.scratch = catalog, str(scratch)
        identity = hashlib.sha256(canonical_json(catalog)).hexdigest()[:20]
        if state.get('catalog_identity',identity)!=identity:
            raise ValueError('closure catalog changed')
        state['catalog_identity']=identity
        state.setdefault('sequence',0)
        state.setdefault('pending',{})
        state.setdefault('results',{})

    def submit(self, operation, **payload):
        self.state['sequence'] += 1
        identity = f"closure-{self.state['catalog_identity']}-{self.state['sequence']}"
        # Freeze JSON now so later report-check mutations cannot alter a durable identity.
        import json
        value = json.loads(canonical_json(dict(payload, catalog=self.catalog, scratch=self.scratch)))
        self.state['pending'][identity] = {'operation':operation,'payload':value}
        # Caller persists state before dispatch via dispatch(); this makes a
        # controller restart replay the same operation ID, never mint a new one.
        return identity

    def dispatch(self):
        for identity, row in self.state['pending'].items():
            self.publisher.submit_operation(identity,row['operation'],row['payload'])

    def poll(self):
        self.publisher.poll()
        completed = []
        for identity, row in list(self.state['pending'].items()):
            result = self.publisher.operation_result(identity)
            if result is None:
                continue
            self.state['results'][identity] = {'operation':row['operation'],
                'sha256':hashlib.sha256(canonical_json(result)).hexdigest()}
            del self.state['pending'][identity]
            completed.append((identity,row['operation'],result))
        return completed


class RuntimeAcknowledgment:
    """P3 admission acknowledgment includes original runtime state, not only money.

    One cohort publisher owns both publications. Snapshot under the local mutex;
    no remote I/O or acknowledgment wait holds it. The opaque reservation ID is
    the stable join key for a restored runtime/ledger pair.
    """
    def __init__(self, store, publisher, watchdog):
        from .accounting_profiles import PublisherAcknowledgment
        self.accounting=PublisherAcknowledgment(store,publisher)
        self.publisher,self.watchdog=publisher,watchdog

    def begin(self, reservation):
        import json
        w=self.watchdog
        with w.mutex:
            if w.state.get('runtime_durability_failure'):
                raise RuntimeError('runtime durability failed')
            w.save()
            checkpoint=json.loads(json.dumps(w.state))
        name='runtime-'+reservation['id']
        self.publisher.submit(name,{'arm':w.path.name,'attempt_id':reservation['id'],
            'runtime':checkpoint})
        ledger=self.accounting.begin(reservation)
        return name,ledger

    def acknowledged(self, names):
        runtime,ledger=names
        return (self.publisher.require_ack(runtime) and self.accounting.acknowledged(ledger)
                and self.watchdog.runtime_durable())
