"""Single-writer watchdog core. Clock and owned-worker factory are injected.
No PID is accepted from disk or a caller. Only handles created by this instance
may be stopped. CLI/paid launch remains gated until the round-4 integration gate.
"""
from dataclasses import asdict
import json
import os
import tempfile
import threading
import secrets
import hashlib
import re
from pathlib import Path
from .control import atomic_json
from .policy import Run, Ledger, changed, error_class, backoff
from .artifacts import scan, report_content
from .control import atomic_json
from .recovery import fingerprint, compaction_failure, DepthScore
from .scope import validate_root
from .consolidation import watch as consolidate
from .runtime_guards import ReportClock


from .solo_closing import SoloClosing


class Watchdog(SoloClosing):
    def __init__(self, root, name, clock, factory, *, lane=None, wall=None, cap=None, margin=0, manifest=None):
        root = validate_root(root, name, manifest)
        self.build_future=None
        self.closure=None
        self.runtime_publisher=None
        self.build_private=None
        self.recovery_message=None  # memory only; never persisted in events/state
        self.path = root/name
        if self.path.is_symlink(): raise ValueError('run symlink refused')
        self.mutex=threading.RLock()
        self.clock=clock; self.factory=factory; self.worker=None; self.margin=margin
        self.path.mkdir(exist_ok=True)
        self.work=self.path/'work'; self.work.mkdir(exist_ok=True)
        if self.work.is_symlink(): raise ValueError('work symlink refused')
        archive=self.work/'notes-archive'
        if archive.is_symlink():raise ValueError('archive symlink refused')
        archive.mkdir(exist_ok=True)
        state_path=self.path/'runtime.json'
        if state_path.exists():
            self.state=json.loads(state_path.read_text())
            self.margin=self.state['stop_margin']
            saved=self.state['run']
            self.run=Run(saved['lane'],saved['start'],saved['wall'],saved['cap'])
            self.run.__dict__.update(saved)
            self.ledger=Ledger.restore(self.state['ledger'])
            if self.ledger.cap != self.run.cap: raise ValueError('budget mismatch')
        else:
            self.run=Run(lane,clock(),wall,cap); self.ledger=Ledger(cap)
            self.state={'report_delivery':'missing-pair','claimed_status':None,'verification_outcome':'not-run','build':{'outcome':'not-run'},'stop_margin':margin,'session':0,'generation':0,'notice':None,'compaction':{},'terminal':None,
                        'delivery_restarts':{},'retry_at':None,'retry_attempt':0,'verifier_feedback':[], 'snapshots':{},'events':[]}
            self.save()
    def save(self):
        self.state.update(run=asdict(self.run),ledger=self.ledger.snapshot())
        publication=None
        if self.runtime_publisher is not None and not self.state.get('runtime_durability_failure'):
            fields=('session','generation','delivery_restarts','retry_attempt','provider_incident',
                    'idle_recoveries','compaction','context_failure_sessions','recovery','gateway_incident',
                    'verification_identity','verification_attestation','internal_status','internal_candidate_hash',
                    'rejected_report_candidates','reply_receipts','report_handover_pending','report_reads_restricted',
                    'terminal','closing','bounded_cancellations','receipt_failure','rejected_consumption_receipts','report_evidence_reads',
                    'pending','request_deadline','worker_restart_pending','worker_retry_at',
                    'closure','closure_builds','closure_statuses','closure_bundles','closure_manifest',
                    'closure_preflight_id','closure_close_id','closure_candidate_builds','selected_candidate_hash',
                    'build','build_pending','pending_verdict',
                    'error_report','error_close_pending','depth_scores','claimed_status',
                    'closing_retry_exhausted','closing_fallback_models','closing_fallback_pending','writer_finalization_pending','final_check_summary',
                    'verification_outcome','exhausted_request_shapes','solo_final_check_required',
                    'final_verification','final_checks','verification_incomplete','board_refresh')
            protected={k:self.state.get(k) for k in fields}
            protected['run']={k:v for k,v in self.state['run'].items() if k not in ('last_write','last_notes')}
            lock=dict(self.state.get('report_lock',{}))
            if lock:
                lock['last_tick']=int(lock['last_tick']//60)
                lock['idle_seconds']=int(lock['idle_seconds']//60)
            protected['report_lock']=lock
            digest=hashlib.sha256(json.dumps(protected,sort_keys=True).encode()).hexdigest()
            if digest!=self.state.get('runtime_checkpoint_digest'):
                self.state['runtime_checkpoint_digest']=digest
                sequence=self.state.get('runtime_checkpoint_sequence',0)+1
                self.state['runtime_checkpoint_sequence']=sequence
                publication=f'runtime-state-{hashlib.sha256(str(self.path).encode()).hexdigest()[:16]}-{sequence:012d}'
                self.state['runtime_publication']=publication
        atomic_json(self.path/'runtime.json',self.state)
        if publication:
            try:self.runtime_publisher.submit(publication,json.loads(json.dumps(self.state)))
            except (OSError, ValueError, RuntimeError) as exc:
                self.state['runtime_durability_failure']=type(exc).__name__
                atomic_json(self.path/'runtime.json',self.state)
        # Derived spend projection covers provider reservations/corrections AND tools.
        # runtime.json remains canonical if interrupted between the two replacements.
        fd,temporary=tempfile.mkstemp(prefix='.spend-',dir=self.path)
        try:
            with os.fdopen(fd,'w') as stream:
                for rid,amount in self.ledger.settled.items():
                    stream.write(json.dumps({'request':rid,'microdollars':amount})+'\n')
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path/'SPEND.log')
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    def attach_runtime_publisher(self, publisher):
        self.runtime_publisher=publisher
        self.state.pop('runtime_checkpoint_digest',None)
        self.save()
    def runtime_durable(self):
        if self.runtime_publisher is None:return True
        if self.state.get('runtime_durability_failure'):return False
        try:
            self.runtime_publisher.poll()
            identity=self.state.get('runtime_publication')
            return bool(identity and self.runtime_publisher.require_ack(identity))
        except (OSError, ValueError, RuntimeError) as exc:
            self.state['runtime_durability_failure']=type(exc).__name__
            self.save()
            return False
    def event(self, kind):
        self.state['events'].append({'kind':kind,'at':self.clock(),'session':self.state['session']})
    def attach_closure(self, publisher, *, catalog, scratch):
        from .runtime_closure import RuntimeClosure
        self.closure = RuntimeClosure(publisher,self.state.setdefault('closure',{}),
                                      catalog=catalog,scratch=scratch)
        if not self.state.get('closure_preflight_id'):
            self.state['closure_preflight_id'] = self.closure.submit('preflight')
        self.save();self.closure.dispatch()
    def closure_poll(self):
        if self.closure is None:return
        if self.state.get('runtime_durability_failure'):return
        try:completed=self.closure.poll()
        except (OSError, ValueError, RuntimeError) as exc:
            self.state['runtime_durability_failure']=type(exc).__name__;self.save();return
        for identity, operation, result in completed:
            if operation=='preflight':
                self.state['closure_ready']=result['fallback_bundle']
            elif operation=='build':
                metadata=dict(result['metadata'],at=self.clock())
                submitted=self.state.get('closure_builds',{}).get(identity,{})
                metadata.update(sha256=submitted.get('sha256'), inputs_sha256=submitted.get('inputs_sha256'))
                self.state['build']=metadata
                self.state['build_pending']=False
                self.state.setdefault('build_history',[]).append(dict(metadata))
                self.build_private=result.get('diagnostics') or None
                if self.build_private and self.worker is not None:
                    self.state['notice']=None
                    self.notice('build-failure')
                if result.get('bundle_id'):
                    self.state.setdefault('closure_bundles',{})[result['candidate_hash']]=result['bundle_id']
                    decision=self.state.get('closure_statuses',{}).get(result['candidate_hash'])
                    if decision:
                        self.closure.submit('status',bundle_id=result['bundle_id'],candidate_hash=result['candidate_hash'],**decision)
                self.report_status()
                if self.state.pop('error_close_pending',False) and metadata.get('outcome')=='pass' and self.state['report_delivery']=='delivered':
                    self.finish('rejected-claim-report')
                pending=self.state.pop('pending_verdict',None)
                if pending and self.run.verify_start is not None:
                    self.state['verification_build_checked']=True
                    self.verdict(True,pending)
            elif operation=='select-final':
                self.state['final_verification']['selected']=result
            elif operation=='close':
                self.state['closure_manifest']=result
                self.state['terminal']={'reason':result['stop_reason'],'spent':self.ledger.spent,
                    'budget':self.ledger.cap,'at':result['stopped_at']}
                self.state['final_build_passes']=result['model_delivery']=='compiling-model-bundle'
                self.event('done')
        self.save()
        if not self.state.get('runtime_durability_failure'):self.closure.dispatch()
    def prepare_reply(self, worker, request_id, outcome):
        """Durably register a completed response before the gateway exposes it."""
        with self.mutex:
            if worker is not self.worker:
                raise ValueError('reply-owner-mismatch')
            if self.clock() >= self.run.start+self.run.wall:
                self.state.setdefault('bounded_cancellations',[]).append(
                    dict(request=request_id,session=worker.session,at=self.clock(),reason='reply-after-original-cutoff'))
                self.save()
                raise ValueError('reply-after-original-cutoff')
            replies = self.state.setdefault('reply_receipts', {})
            if request_id in replies:
                raise ValueError('duplicate-response-delivery')
            token = secrets.token_hex(32)
            replies[request_id] = dict(token=token, session=worker.session,
                                      model=worker.model, outcome=outcome,
                                      completed_at=self.clock(), consumed_at=None)
            self.save()
            return token
    def consume_reply(self, worker, event):
        with self.mutex:
            if worker is not self.worker or event.get('session') != worker.session:
                raise ValueError('receipt-owner-mismatch')
            matches = [row for row in self.state.get('reply_receipts', {}).values()
                       if row['token'] == event.get('response_id')]
            if len(matches) != 1:
                # A failed client receipt must remain diagnosable after emergency
                # reap. Never acknowledge it or edit a registered paid receipt.
                self.state['receipt_failure'] = 'unregistered-response-receipt'
                self.state.setdefault('rejected_consumption_receipts', []).append(
                    dict(response_id=event.get('response_id'), session=event.get('session'),
                         model=event.get('model'), at=self.clock()))
                self.save()
                raise ValueError('unregistered-response-receipt')
            row = matches[0]
            if row['session'] != worker.session or row['model'] != event.get('model'):
                raise ValueError('receipt-identity-mismatch')
            if row['consumed_at'] is not None:
                self.state['receipt_failure'] = 'duplicate-consumption'
                self.save()
                raise ValueError('duplicate-consumption')
            row['consumed_at'] = self.clock()
            if getattr(self,'accounting',None) is not None:
                request_id=next(key for key,value in self.state['reply_receipts'].items() if value is row)
                with self.accounting.transaction() as db:
                    self.accounting.event(db,'reply-consumed',dict(id=request_id,at=row['consumed_at'],outcome=row['outcome']))
            if row['outcome'] == 'normal':
                self.state['idle_recoveries'] = 0
                self.state.pop('provider_incident',None)
            self.save()
    def awaiting_reply(self):
        return any(row['session'] == self.state['session'] and row['outcome']=='normal' and row['consumed_at'] is None
                   and row.get('abandoned_at') is None for row in self.state.get('reply_receipts', {}).values())
    def enter_report_mode(self, reason):
        """Commit admission lock first; fence the previous namespace at handover."""
        first = not self.state.get('report_lock')
        if self.worker is not None and hasattr(self.worker,'fence_research_tools'):
            self.worker.fence_research_tools()
        clock = ReportClock(self.state.setdefault('report_lock', {}))
        clock.lock(self.clock(), reason, self.run.start+self.run.wall)
        self.run.writeup = True
        if first and reason=='research-pool-limit' and self.state.get('admission_stop') in ('budget','liability-limit'):
            self.state.pop('admission_stop',None)
        if first:
            self.state['report_handover_pending'] = True
            self.run.notice_due = self.clock()
            self.run.notice_delivered = False
            self.event('report-lock')
        self.save()
    def report_handover(self):
        if not self.state.get('report_handover_pending'):
            return
        if self.busy() or self.awaiting_reply():
            return
        self.stop_worker()
        incident=self.state.get('gateway_incident')
        if incident and not incident.get('complete'):
            incident.update(complete=True,closed_by='report-handover')
        self.state['report_handover_pending'] = False
        self.run.phase = 'verification' if self.run.verify_start is not None else 'write-up'
        self.save()
        self.start()
    def role_admission(self, worker):
        """Content-free gateway admission gate; called before every request.

        False asks the controller to complete handover/restriction/closure. It
        never kills a worker from its own request thread or renews a clock.
        """
        if worker is not self.worker or self.state['terminal'] or self.state.get('closing') or self.state.get('receipt_failure'):
            return False
        if self.clock() >= self.run.start+self.run.wall:
            return False
        if getattr(worker,'role',None)=='internal-verifier' and getattr(worker,'capability_role','research')!='research':
            if report_content(worker.role_output,'VERIFICATION.json',65536):return False
        if self.state.get('final_verification', {}).get('phase') == 'checking':
            return getattr(worker, 'role', None) == 'internal-verifier' and self.clock() < self.run.verify_start+1800
        if self.state.get('report_lock'):
            if self.state.get('report_handover_pending') or getattr(worker,'role',None) == 'research':
                return False
            clock = ReportClock(self.state['report_lock'])
            action = clock.advance(self.clock(), revision=self.report_revision())
            self.save()
            if action == 'stop' or (action == 'restrict' and not self.state.get('report_reads_restricted')):
                return False
        return True
    def report_revision(self):
        # A stable complete draft's content change can count; mtime, whitespace,
        # comment padding, acknowledgments and unrelated growth cannot.
        source = report_content(self.work, 'FINAL.tex')
        if not self.report_ready():
            return None
        source = re.sub(rb'(?<!\\)%[^\n]*', b'', source)
        end=source.find(b'\\end{document}')
        if end<0:return None
        source=source[:end+len(b'\\end{document}')]
        source = re.sub(rb'\s+', b'', source)
        return hashlib.sha256(source).hexdigest()
    def start(self):
        if self.state.get('final_verification',{}).get('phase') in ('prepare','done'):return
        if self.worker is not None: raise RuntimeError('worker already owned')
        if self.state.get('pending'):
            self.state['worker_restart_pending']=True;self.save();return
        if self.state.get('runtime_durability_failure'):return
        if self.state.get('receipt_failure'):
            self.finish('reply-consumption-failure');return
        if self.state['terminal'] or self.run.phase in ('done','paused-alert') or self.run.outage_start is not None: return
        if self.closure is not None and not self.state.get('closure_ready'):return
        if self.run.writeup and not self.state.get('report_lock'):
            self.enter_report_mode('restored-write-up')
        if self.state.get('report_handover_pending'):return
        if (self.state.get('worker_retry_at') or 0) > self.clock(): return
        final_check = self.state.get('final_verification', {}).get('phase') == 'checking'
        # Spend-then-assess (2026-10-05): no worst-request margin; money never
        # ends the run here (live solo check 8: K3's $14.35 margin ended its writer).
        if self.clock() >= self.run.start+self.run.wall:
            self.finish('deadline'); return
        if self.state.get('pending_verdict') and self.state.get('build_pending'):return
        if self.state.get('report_lock'):
            self.run.writeup = True
            if self.state['report_lock'].get('stopped') and not final_check:
                self.finish('report-inactivity'); return
        if not final_check and self.state.get('minimal_report') and not self.report_pair_ready() and self.clock()-self.state['minimal_started']>=600:
            self.pause('minimal-report-timeout');return
        self.state['worker_retry_at']=None
        self.state.pop('worker_restart_pending',None)
        self.state['session']+=1
        self.state['session_depth']={'tokens':0,'compacted':False}
        self.state['verification_cause']=None
        self.run.restart(self.clock())
        # Existing sticky write-up is in the FIRST message, not a queue behind a goal.
        pending=self.state['notice']
        self.state['notice']=None
        if self.run.writeup:self.run.notice_due=self.clock()
        if not self.state.get('report_lock') and consolidate(self,True):
            c=self.state['consolidation']
            kind='consolidate-repeat' if c['status']=='repeat' else 'consolidate'
            if pending and pending['kind']==kind:
                self.state['notice']=pending;pending['attached']=False
            else:self.notice(kind)
        elif self.run.writeup:
            self.notice('audit-polish' if self.run.audit else self.state.get('writeup_kind','write-up')); self.run.notice_due=self.clock()
        elif pending and not pending['attached']:
            self.notice(pending['kind'])
        self.save()
        delivery=dict(self.state['notice']) if self.state['notice'] else None
        if delivery and self.build_private:delivery['private_message']=self.build_private
        if self.recovery_message:
            delivery=delivery or {'kind':'checkpoint','generation':max(1,self.state['generation'])}
            delivery['recovery_message']=self.recovery_message
        work = self.work
        if self.run.verify_start is not None:
            from .verification import snapshot
            work = self.path/f'verifier-{self.state["session"]:04d}'
            work.mkdir()
            try:
                digest, entries = snapshot(self.verification_source(), work)
            except (OSError, ValueError):
                self.verdict(False, 'insufficient-verification-resources'); return
            identity = self.state.get('verification_identity')
            if identity and identity['cycle'] == self.run.cycles and identity['candidate_hash'] != digest:
                self.verdict(False, 'concrete-actionable-defect'); return
            self.state['verification_identity'] = dict(session=self.state['session'],
                cycle=self.run.cycles, candidate_hash=digest, work=str(work), started_at=self.run.verify_start)
            from .runtime_closure import candidate_hash
            try:self.state['verification_identity']['report_candidate_hash']=candidate_hash(work)
            except (OSError, ValueError):pass
            from .closure_build import report_dependencies
            from .final_checks import inventory
            rendered=report_content(work, 'FINAL.pdf', 32*1024*1024)
            try:
                files=report_dependencies(work)
                report_hash=self.state['verification_identity'].get('report_candidate_hash')
                bundle=self.state.get('closure_bundles',{}).get(report_hash)
                if bundle and self.closure is not None and not final_check:
                    pdf=Path(self.closure.catalog['artifact_root'])/'bundles'/bundle/'FINAL.pdf'
                    if pdf.is_file():rendered=pdf.read_bytes()
                final_inventory=inventory(files,rendered=rendered)
            except (OSError,ValueError):final_inventory=inventory({})
            self.state['verification_identity']['final_inventory']=final_inventory
            atomic_json(work/'FINAL-INVENTORY.json',final_inventory)
            if rendered:(work/'REVIEWED-FINAL.pdf').write_bytes(rendered)
            self.save()
        self.worker=self.factory(work,self.state['session'],self.run.phase,delivery)
        if self.run.verify_start is not None:
            from .runtime import MODELS
            expected = MODELS[self.run.lane][0]
            if getattr(self.worker, 'model', None) != expected or getattr(self.worker, 'role', None) != 'internal-verifier':
                self.pause('verifier-identity-mismatch'); return
            self.state['verification_identity']['model'] = expected
            self.state['verification_identity']['output'] = str(getattr(self.worker,'role_output',work))
            self.state['verification_identity']['record_name'] = 'VERIFICATION.json'
            if self.state.get('report_lock'):
                ReportClock(self.state['report_lock']).verifier(self.clock(),self.run.verify_start+1800)
        elif self.state.get('report_lock'):
            clock = ReportClock(self.state['report_lock'])
            if clock.state['ready_at'] is None:
                clock.state['revision'] = self.report_revision()
            clock.ready(self.clock())
            clock.author(self.clock())
        self.recovery_message=None
        self.event('start'); self.save()
    def stop_worker(self):
        if self.worker is not None:
            for request in self.state.get('pending',{}):
                cancellations=self.state.setdefault('bounded_cancellations',[])
                if not any(row['request']==request for row in cancellations):
                    cancellations.append(dict(request=request,session=self.worker.session,
                        at=self.clock(),reason=self.state.get('closing') or 'owned-worker-fence'))
            self.save()
            self.worker.stop()  # must terminate namespace and wait; no arbitrary PID API
            if self.worker.alive(): raise RuntimeError('cleanup failed')
            self.promote_report(self.worker)
            # Preserve terminal/no-next-turn receipts already emitted before reap.
            if hasattr(self, 'consume_fenced_events'):
                self.consume_fenced_events(self.worker)
            elif hasattr(self.worker, 'drain'):
                for event in self.worker.drain():
                    if event['kind'] == 'reply-consumed':
                        self.consume_reply(self.worker, event)
            if self.awaiting_reply():
                # Never mask an earlier, graver receipt failure (duplicate consumption).
                self.state.setdefault('receipt_failure', 'missing-consumption')
            self.worker=None
            self.event('reaped')
    def report_cap_failure(self, session=None):
        """Solo authors reject any cap, including one followed by a normal retry."""
        from .reply_outcomes import any_capped_session
        return any_capped_session(self.state, session)

    def promote_report(self, worker):
        """Copy only bounded allowed output bytes from the currently owned author."""
        if worker is not self.worker or getattr(worker,'role',None)!='report-author':
            return
        output = getattr(worker,'role_output',None)
        if output is None:
            return
        cap=self.report_cap_failure(worker.session)
        if cap:
            # A capped author remains ineligible for normal promotion, but its
            # saved source is still the latest deadline draft, explicitly failed.
            data=report_content(output,'FINAL.tex',1024*1024)
            if data:
                from .report_gate import _atomic,DRAFT,DRAFT_PROVENANCE
                self.state.setdefault('report_check',{})['author_cap_failure']=cap
                _atomic(self.work/DRAFT,data)
                provenance=report_content(output,'PROVENANCE.md',1024*1024)
                _atomic(self.work/DRAFT_PROVENANCE,provenance)
                self.retain_delivery_draft({'FINAL.tex':data,'PROVENANCE.md':provenance},
                    captured_at_ns=(Path(output)/'FINAL.tex').stat().st_mtime_ns,checks=dict(author_cap_failure=cap))
                self.save()
            return
        try:
            used=json.loads(report_content(output,'.evidence-read-budget.json',128))
            if type(used) is int and 0<=used<=16:
                self.state['report_evidence_reads']=max(used,self.state.get('report_evidence_reads',0))
        except (ValueError,UnicodeError):pass
        for name in ('FINAL.tex','PROVENANCE.md','AUDIT.md','BLOCKED.md'):
            if name=='PROVENANCE.md' and getattr(self,'report_gate_on',False):continue
            data = report_content(output,name,1024*1024)
            if data and name=='FINAL.tex' and getattr(self,'report_gate_on',False):
                # Final-report check (v2/report_gate.py): only a passing draft reaches the slot.
                from .report_gate import gate_promotion
                try:data=gate_promotion(self,data,report_content(output,'PROVENANCE.md',1024*1024))
                except Exception as exc:
                    self.state.setdefault('report_check',{})['error']=type(exc).__name__+': '+str(exc)[:200]
                self.save()
            if not data:continue
            same=data==report_content(self.work,name,1024*1024)
            if same:
                if name=='FINAL.tex':self.retain_promoted_draft(data,output)
                continue
            fd, temporary = tempfile.mkstemp(prefix='.report-',dir=self.work)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,self.work/name)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
            if name=='FINAL.tex':self.retain_promoted_draft(data,output)

    def retain_promoted_draft(self,data,output):
        # Capture the dependency closure, including the current authority, even
        # when an identical source was saved again and needs no slot replacement.
        from .closure_build import report_dependencies
        try:files=report_dependencies(self.work)
        except (OSError,ValueError):files={'FINAL.tex':data,'PROVENANCE.md':report_content(output,'PROVENANCE.md',1024*1024)}
        self.retain_delivery_draft(files,captured_at_ns=(Path(output)/'FINAL.tex').stat().st_mtime_ns)

    def retain_delivery_draft(self, files, *, model_id=None, captured_at_ns=None, copy=False, checks=None):
        """Queue immutable bytes before compiling; failed checks never erase them.
        copy=True: a work/promotion copy, whose mtime is not an author save."""
        owner=getattr(self,'owner',None)
        target=self if getattr(self,'closure',None) is not None else getattr(owner,'watchdog',None)
        if target is None or target.closure is None:return
        import base64, time
        from .runtime import MODELS
        digest=hashlib.sha256(json.dumps({k:hashlib.sha256(v).hexdigest() for k,v in sorted(files.items())},sort_keys=True).encode()).hexdigest()
        stamp=captured_at_ns or time.time_ns()
        if (self.state.get('delivery_draft_digest')==digest and
                (copy or self.state.get('delivery_draft_captured_at_ns',0)>=stamp)):return
        # A report check is attached only to the exact bytes it judged.
        from .report_gate import gate_digest
        check=self.state.get('report_check') or {}
        if checks is None and check.get('last_digest'):
            try:
                if check['last_digest']==gate_digest(self.work,files.get('FINAL.tex',b''),files.get('PROVENANCE.md') or b''):checks=check
            except (OSError,ValueError):pass
        target.closure.submit('retain-draft', files={k:base64.b64encode(v).decode() for k,v in files.items()},
            author_id=getattr(target,'author_id',target.path.name),
            model_id=model_id or (self.context.model if getattr(self,'context',None) else MODELS[self.run.lane][0]),
            captured_at_ns=stamp,checks=checks,copy=copy)
        self.state['delivery_draft_digest']=digest
        self.state['delivery_draft_captured_at_ns']=stamp
        self.save();target.save();target.closure.dispatch()

    def finish(self, reason):
        if not self.final_check_gate(reason):return
        # Persist closed admission before cleanup; done published only after reaping.
        self.run.phase='done'; self.state['closing']=reason; self.save()
        self.stop_worker()
        if self.state.get('pending'):return
        if self.closure is not None:
            draft=self.work/'FINAL.tex'
            if draft.exists():
                from .deadline_writeup import model_text
                data=report_content(self.work,'FINAL.tex',1024*1024)
                if model_text(data):
                    from .closure_build import report_dependencies
                    try:files=report_dependencies(self.work)
                    except (OSError,ValueError):files={'FINAL.tex':data}
                    self.retain_delivery_draft(files,captured_at_ns=draft.stat().st_mtime_ns,copy=True)
            if self.closure.state['pending']:return
            # A model-written draft present at freeze is the report, never the
            # administrative placeholder (coordinator, after live solo check 8):
            # compile the latest FINAL.tex once before closing.
            draft=self.work/'FINAL.tex'
            if (not self.state.get('closure_close_id') and not self.state.get('closure_bundles')
                    and not self.state.get('selected_candidate_hash')
                    and not self.state.get('final_draft_build') and draft.exists() and draft.stat().st_size>1000):
                self.state['final_draft_build']=self.clock();self.save()
                self.request_build('final-draft-at-freeze')
                return
            if self.state.get('build_pending'):return
            # Operator ruling ~15:25 PDT: if the primary writer has no compiled
            # report, compile the Flash backup writer's draft (its own directory).
            backup=self.state.get('backup_work')
            if (backup and not self.state.get('closure_close_id') and not self.state.get('closure_bundles')
                    and not self.state.get('selected_candidate_hash')
                    and not self.state.get('backup_build') and (Path(backup)/'FINAL.tex').exists()
                    and (Path(backup)/'FINAL.tex').stat().st_size>1000):
                self.state['backup_build']=self.clock();self.save()
                self.request_build('backup-draft-at-freeze',source=backup,model_id=self.state.get('backup_model_id','deepseek/deepseek-v4.1-flash'))
                return
            if not self.state.get('closure_close_id'):
                lock=self.state.get('report_lock',{})
                cause='no-author-ready-opportunity' if lock and lock.get('ready_at') is None else None
                from .runtime import MODELS
                drafts=[dict(root=str(self.work),author_id=getattr(self,'author_id',self.path.name),
                             model_id=MODELS[self.run.lane][0],checks=self.state.get('report_check',{}))]
                if backup:drafts.append(dict(root=str(backup),author_id=getattr(self,'author_id',self.path.name),
                    model_id=self.state.get('backup_model_id','deepseek/deepseek-v4.1-flash')))
                self.state['closure_close_id']=self.closure.submit('close',reason=reason,
                    draft_sources=drafts,
                    stopped_at=self.clock(),operational_cause=cause,
                    reviewed_bundle=(self.state.get('closure_bundles',{}).get(self.state['selected_candidate_hash'])
                        if self.state.get('selected_candidate_hash') else
                        self.state.get('final_verification',{}).get('selected',{}).get('bundle_id')),
                    verification_status=(self.state.get('internal_status') if self.state.get('selected_candidate_hash') else
                        self.state.get('final_verification',{}).get('status')),
                    required_claim_manifest_sha256=self.state.get('required_claim_manifest_sha256'),
                    check_summary=self.state.get('final_check_summary'))
                self.save();self.closure.dispatch()
            return
        if self.build_future is not None:
            result,_=self.build_future.result(timeout=125);self.build_future=None
            self.state['build']=dict(result,at=self.clock());self.state['build_pending']=False
            self.state.setdefault('build_history',[]).append(dict(self.state['build']))
        self.report_status()
        self.state['final_build_passes']=self.state['report_delivery']=='delivered' and self.state.get('build',{}).get('outcome')=='pass'
        self.state['terminal']={'reason':reason,'spent':self.ledger.spent,'budget':self.ledger.cap,'at':self.clock()}
        self.event('done'); self.save()
    def pause(self, reason):
        self.run.phase='paused-alert'; self.save(); self.stop_worker(); self.event(reason); self.save()
    def restart(self, reason):
        self.state.pop('depth_restart_due',None)
        self.stop_worker(); self.event(reason); self.save(); self.start()
    def notice(self, kind):
        if self.run.writeup and not kind.startswith(('write-up','consolidate','audit-polish','build-failure')): return
        if self.state['notice'] and self.state['notice']['kind']==kind: return
        self.state['generation']+=1
        self.state['notice']={'kind':kind,'generation':self.state['generation'],'at':self.clock(),'attached':False}
        self.state['requests_since_notice']=0
        self.save()
        observations=self.state.setdefault('notice_observations',{})
        observations[str(self.state['generation'])]={'kind':kind,'session':self.state['session'],'at':self.clock(),
            'depth':self.state.get('session_depth',{}).get('tokens',0),
            'compacted':self.state.get('session_depth',{}).get('compacted',False),
            'attached':False,'acknowledged':False,'written':False,'files':[], 'outage':False,
            'eligible_seconds':0,'last_observed_at':self.clock()}
        self.save()
        if self.worker is not None:
            delivery=dict(self.state['notice'])
            if self.build_private:delivery['private_message']=self.build_private
            self.worker.notice(delivery)
            normalized='write-up' if kind.startswith('write-up') else 'checkpoint' if kind=='checkpoint-final' else kind
            if self.state.get('depth_scores',{}).get(normalized,{}).get('restart_delivery'):
                # Deliver in a fresh session, but never kill a running request or tool.
                if self.busy():self.state['depth_restart_due']=True;self.save()
                else:self.restart('depth-delivery-fallback')
    def request_in_flight(self):
        # Long reasoning requests run up to ~30 min; never kill one mid-flight, but bound the wait.
        since=self.state.get('inflight_since')
        deadline = self.state.get('request_deadline')
        if deadline is None and since is not None:
            deadline = min(since+__import__('v2.gateway',fromlist=['x']).request_deadline_seconds(getattr(getattr(self,'context',None),'model','')), self.run.start+self.run.wall)
        return (bool(self.state.get('pending')) and self.worker is not None and
                deadline is not None and self.clock()<deadline)
    def busy(self):
        # Starts/ends come from the owned client stream, never a model's assertion.
        # Protect short tools too; an open socket without admission is not activity.
        if self.request_in_flight():return True
        age=self.worker.tool_age() if self.worker is not None and hasattr(self.worker,'tool_age') else None
        return age is not None and 0<=age<1800 and self.clock()<self.run.start+self.run.wall
    def missing_notice(self):
        # A notice attaches at the model's next tool call. Time spent inside a model request
        # (up to ~30 min for long reasoning) is not a delivery failure: wait for the request
        # to end, then allow the usual 240 s for the next tool call.
        if self.busy():return
        ended=self.state.get('last_request_end')
        if ended is not None and self.clock()-ended<240:return
        kind=self.state['notice']['kind'] if self.state['notice'] else 'write-up'
        # A worker that stopped requesting (e.g. its goal paused after a reasoning-only reply)
        # is idle, not a delivery failure: restart from disk without counting a miss.
        if self.state['notice'] and self.state.get('requests_since_notice',0)<=1:
            count = self.state.get('idle_recoveries', 0)
            if count >= 3:
                self.pause('idle-recovery-exhausted'); return
            self.state['idle_recoveries'] = count + 1
            self.event('idle-restart');self.restart('idle-after-response');return
        attempts=self.state['delivery_restarts'].get(kind,0)
        if attempts>=1:
            if self.run.writeup and not self.report_ready():self.minimal_report('notice-delivery-exhausted')
            else:self.pause('notice-delivery-exhausted')
        else:
            self.state['delivery_restarts'][kind]=attempts+1
            self.restart('notice-attachment-timeout')
    def receipt(self, generation):
        notice=self.state['notice']
        if notice and generation==notice['generation']:
            notice['attached']=True
            # A delivered notice clears earlier misses of its kind; only consecutive misses pause.
            self.state['delivery_restarts'].pop(notice['kind'],None)
            self.state.pop('depth_restart_due',None)  # delivered in-session after all
            observation=self.state.get('notice_observations',{}).get(str(generation))
            if observation: observation.update(attached=True,attached_at=self.clock())
            if notice['kind'].startswith('write-up') or notice['kind']=='audit-polish':
                self.run.notice_delivered=True
                self.state.setdefault('writeup_delivered_at',self.clock())
            self.save()
    def settle(self, request, amount):
        self.ledger.settle(request,amount); self.save()
    def supervisor_crashed(self):
        action=self.run.crash(self.clock(),self.worker is not None and self.worker.alive())
        self.save(); self.stop_worker()
        if action=='pause-alert': self.pause('crash-exhausted')
        else: self.start()
    def worker_exited(self, evidence=None):
        evidence = evidence or {}
        kind = evidence.get('kind')
        self.state['last_exit'] = {'kind': kind or 'unexplained', 'code': evidence.get('code')}
        if kind == 'supervisor':
            self.supervisor_crashed(); return
        self.stop_worker()
        if kind == 'loop':
            action = self.failed_operation(evidence['tool'], evidence['arguments'])
            if action == 'bounded-recovery': self.start()
        elif kind == 'tool-cap':
            self.event('legacy-tool-cap-ignored');self.start() # v2's CLI tool limit is unlimited
        elif kind == 'context': self.compact(evidence.get('info', {'compressionStatus':7}))
        elif kind == 'provider': self.provider_error(evidence['status'])
        elif kind == 'goal':
            self.goal(evidence['record'])
            if self.worker is None and self.run.phase != 'paused-alert': self.start()
        else:
            action = self.run.worker_crash(self.clock())
            if action == 'pause-alert': self.pause('worker-crash-exhausted')
            else:
                self.state['worker_retry_at'] = self.clock()+backoff(len(self.run.worker_crashes)-1)
                self.event('worker-crash-backoff')
        self.save()
    def consume(self):
        worker = self.worker
        if worker is None or not hasattr(worker, 'drain'): return
        for event in worker.drain():
            if self.worker is not worker: break
            kind = event['kind']
            if kind == 'reply-consumed':
                self.consume_reply(worker, event)
            elif kind == 'receipt':
                self.receipt(event['generation'])
                observation=self.state.get('notice_observations',{}).get(str(event['generation']))
                if observation: observation['receipt_source']=event.get('source','hook')
            elif kind in ('ack','emitted'):
                observation=self.state.get('notice_observations',{}).get(str(event['generation']))
                if observation:
                    observation.setdefault(kind+'_at',self.clock())
                    if kind=='ack': observation['acknowledged']=True
                    self.save()
            elif kind == 'compact': self.compact(event['info'])
            elif kind == 'verification-submitted':
                self.verification_record(True)
            elif kind == 'report-turn-complete':
                self.promote_report(worker)
                if self.report_pair_ready():self.request_build('author-turn-complete')
            elif kind == 'goal':
                record = event['record']
                if self.run.verify_start is not None:
                    cause=record.get('cause')
                    if cause:
                        self.state['verifier_feedback'].append(record)
                        self.state['verification_cause']=cause
                        self.save()
                    if cause=='verifier_reject': self.verification_record(False)
                    elif record['status'] in ('complete','blocked'):
                        self.verification_record(record['status']=='complete' and self.state.get('verification_cause')=='verifier_accept')
                else: self.goal(record)
            elif kind == 'provider':
                self.stop_worker(); self.provider_error(event['status'])
            elif kind == 'loop': self.worker_exited(event)
    def provider_error(self, status):
        kind=error_class(status)
        if kind=='configuration': self.pause('configuration'); return
        incident = self.state.setdefault('provider_incident', {'start': self.clock(), 'failures': 0})
        incident['failures'] += 1
        if incident['failures'] >= 3 or self.clock() >= min(incident['start']+__import__('v2.gateway',fromlist=['x']).request_deadline_seconds(getattr(getattr(self,'context',None),'model','')),self.run.start+self.run.wall):
            self.pause('provider-recovery-exhausted'); return
        self.run.billing(self.clock(),kind)
        self.state['retry_at']=self.clock()+backoff(self.state['retry_attempt'])
        self.state['retry_attempt']+=1
        self.save()
    def provider_healthy(self):
        self.run.healthy(self.clock()); self.state['retry_at']=None; self.state['retry_attempt']=0
        self.state.pop('provider_incident', None)
        if self.worker is None: self.start()
        self.save()
    def report_ready(self):
        data=report_content(self.work,'FINAL.tex')
        return b'\\begin{document}' in data and b'\\end{document}' in data
    def report_pair_ready(self):
        return self.report_ready() and bool(report_content(self.work,'AUDIT.md'))
    def report_status(self):
        import hashlib
        now=self.clock()
        for name,key in [('FINAL.tex','initial_report_at'),('AUDIT.md','initial_audit_at')]:
            if report_content(self.work,name):self.state.setdefault(key,now)
        if self.report_ready():self.state.setdefault('document_marker_at',now)
        pair=self.report_pair_ready()
        self.state['report_presence']='structurally-valid' if self.report_ready() else 'missing-or-invalid'
        self.state['reviewable_pair']=pair
        if pair:self.state.setdefault('reviewable_pair_at',now)
        digest=hashlib.sha256(report_content(self.work,'FINAL.tex')).hexdigest()
        from .tex import input_signature
        build=self.state.get('build',{})
        inputs=input_signature(self.work)
        matches=build.get('sha256')==digest and inputs is not None and build.get('inputs_sha256')==inputs
        if build.get('outcome')=='pass' and not matches:
            build['outcome']='fail-error-class';build['error_class']='source-changed'
        delivered=pair and matches and build.get('outcome') in ('pass','environment-missing')
        self.state['report_delivery']='delivered' if delivered else 'pair-unbuilt' if pair else 'missing-pair'
        if delivered:self.state.setdefault('first_delivery_at',now)
        return pair
    def request_build(self, reason, *, source=None, model_id=None, files=None):
        if self.build_future is not None:return
        if self.closure is not None:
            if self.state.get('build_pending'):return
            from .closure_build import report_dependencies
            from .tex import input_signature
            try:
                files=report_dependencies(Path(source) if source else self.work) if files is None else dict(files)
                directory=Path(tempfile.mkdtemp(prefix='build-source-',dir=self.path))
                for name,data in files.items():
                    target=directory/name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(data);target.chmod(0o444)
            except (OSError, ValueError):
                data=report_content(Path(source) if source else self.work,'FINAL.tex',1024*1024)
                if data:self.retain_delivery_draft({'FINAL.tex':data},model_id=model_id,copy=True)
                self.state['build']={'outcome':'fail','error_class':'dependency'};self.save();return
            from .runtime import MODELS
            # Report-gate administrative text is not a model report, even when
            # padded beyond the structural minimum. Bind this controller fact into
            # the immutable compiler/catalog metadata.
            from .report_gate import fallback_bytes
            try:gate_fallback=files['FINAL.tex']==fallback_bytes(Path(source) if source else self.work)
            except (OSError, ValueError):gate_fallback=False
            import time
            original=Path(source) if source else self.work
            captured_at_ns=(original/'FINAL.tex').stat().st_mtime_ns if (original/'FINAL.tex').exists() else time.time_ns()
            if not gate_fallback:self.retain_delivery_draft(files,model_id=model_id,captured_at_ns=captured_at_ns,copy=True)
            identity=self.closure.submit('build',source=str(directory),report_gate_fallback=gate_fallback,captured_at_ns=captured_at_ns,copy=True,
                author_id=getattr(self,'author_id',self.path.name),model_id=model_id or MODELS[self.run.lane][0])
            self.state.setdefault('closure_builds',{})[identity]={
                'sha256':hashlib.sha256(files['FINAL.tex']).hexdigest(),
                'inputs_sha256':input_signature(Path(source) if source else self.work)}
            if reason=='selected-final-candidate':
                from .final_checks import inventory
                self.state.setdefault('closure_candidate_builds',{})[inventory(files)['candidate_sha256']]=identity
            self.state.update(build_started_at=self.clock(),build_reason=reason,build_pending=True)
            self.save();self.closure.dispatch();return
        from concurrent.futures import ThreadPoolExecutor
        from .tex import build
        if reason!='restart-pending' or 'build_started_at' not in self.state:
            self.state['build_started_at']=self.clock()
        self.state['build_reason']=reason
        self.state['build_pending']=True
        self.save()
        executor=ThreadPoolExecutor(max_workers=1)
        self.build_future=executor.submit(build,self.work)
        executor.shutdown(wait=False)
    def build_watch(self):
        now=self.clock()
        pair=self.report_status()
        if self.build_future is not None and self.build_future.done():
            result,private=self.build_future.result();self.build_future=None
            result['at']=now
            self.state['build']=result;self.state['build_pending']=False
            self.state.setdefault('build_history',[]).append(dict(result))
            self.state['next_build_at']=self.state.get('build_started_at',now)+900
            self.report_status();self.event('build-'+result['outcome'])
            if self.state.pop('error_close_pending',False) and result['outcome']=='pass' and self.state['report_delivery']=='delivered':
                self.finish('rejected-claim-report');return
            pending=self.state.pop('pending_verdict',None)
            if pending and self.run.verify_start is not None:
                self.state['verification_build_checked']=True
                self.verdict(True,pending)
                if self.state['terminal']:return
            if result['outcome']=='pass':self.build_private=None
            if result['outcome']!='pass':
                self.build_private=private
                # Private diagnostics never enter runtime.json or public events.
                self.state['notice']=None
                self.notice('build-failure')
                if self.worker is not None:
                    self.worker.notice(dict(self.state['notice'],private_message=private))
        boundary=now>=self.run.start+.95*self.run.wall or self.ledger.spent>=.95*self.ledger.cap
        # Audit only a report written after the write-up notice and then left unchanged for 15 minutes:
        # a skeleton, or a report that predates write-up or a rejection, is not finished.
        import hashlib
        digest=hashlib.sha256(report_content(self.work,'FINAL.tex')).hexdigest()
        if digest!=self.state.get('report_digest'):
            self.state['report_changed_at']=now if 'report_digest' in self.state else 0
            self.state['report_digest']=digest
        delivered=self.state.get('writeup_delivered_at')
        settled=(pair and delivered is not None and self.state['report_changed_at']>delivered
                 and now-self.state['report_changed_at']>=900)
        if self.run.writeup and (boundary or settled) and not self.run.audit:
            if pair:
                self.run.audit=True;self.state['audit_started_at']=now
                self.state['audit_reason']='95-percent' if boundary else 'report-ready'
                self.notice('audit-polish');self.event('audit-polish')
            elif boundary:
                self.state['audit_not_reached']='missing-report-pair'
                if not self.state.get('minimal_report'):self.minimal_report('audit-missing-pair')
        reason=None
        if boundary and not self.state.get('audit_build_requested'):
            reason='95-percent'
        started=self.state.get('writeup_delivered_at')
        if started is not None and now>=started+600 and not self.state.get('receipt_build_requested'):
            reason=reason or 'receipt-plus-10'
        if self.run.audit and self.state.get('build',{}).get('outcome')!='pass' and now>=self.state.get('next_build_at',float('inf')):
            reason=reason or 'audit-retry'
        if self.state.get('build_pending') and self.build_future is None and self.closure is None:reason='restart-pending'
        if reason and self.build_future is None:
            if boundary:self.state['audit_build_requested']=True
            if started is not None and now>=started+600:self.state['receipt_build_requested']=True
            self.request_build(reason)
        self.save()
    def minimal_report(self, reason):
        self.enter_report_mode(reason)
        if self.state.get('minimal_report'):
            self.pause('minimal-report-failed'); return
        self.state['minimal_report']=True
        self.state['minimal_started']=self.clock()
        self.state['writeup_kind']='write-up-minimal'
        self.run.phase='write-up'; self.run.verify_start=None; self.run.recovery_start=None
        self.restart('minimal-report')
    def writeup_watch(self):
        if self.state.get('report_lock'):
            clock = ReportClock(self.state['report_lock'])
            action = clock.advance(self.clock(), revision=self.report_revision(),
                request_active=self.request_in_flight() or self.awaiting_reply(),
                writes_pending=self.busy())
            self.save()
            if action == 'stop':
                self.finish('report-inactivity')
            elif action == 'restrict' and not self.state.get('report_reads_restricted'):
                self.state['report_reads_restricted'] = True
                self.restart('report-reads-restricted')
            return
        if self.state.get('minimal_report') and not self.report_pair_ready() and self.clock()-self.state['minimal_started']>=600:
            self.pause('minimal-report-timeout');return
        started=self.state.get('writeup_delivered_at')
        if started is None or self.report_pair_ready() or self.run.outage_start is not None:return
        age=self.clock()-started
        if self.state.get('minimal_report'):
            if self.clock()-self.state['minimal_started']>=600:self.pause('minimal-report-timeout')
        elif age>=2400:self.minimal_report('report-overdue')
        elif age>=1200 and not self.state.get('writeup_fresh'):
            self.state['writeup_fresh']=True; self.state['writeup_kind']='write-up-fresh'
            self.restart('write-up-report-first')
        elif age>=600 and not self.state.get('writeup_targeted'):
            self.state['writeup_targeted']=True; self.state['writeup_kind']='write-up-targeted'
            self.notice('write-up-targeted')
    def early_writeup(self, exhausted=False):
        if exhausted:self.state['loop_recovery_exhausted']=True
        pressure=self.state.get('loop_recovery_exhausted') or len(self.state.get('context_failure_sessions',[]))>=2
        if pressure and not self.run.writeup and (self.clock()-self.run.start>=.75*self.run.wall or self.ledger.spent>=.75*self.ledger.cap):
            self.enter_report_mode('failure-pressure');self.run.phase='write-up'
            self.run.recovery_start=None;self.event('early-write-up');self.save()
        return self.run.writeup
    def failed_operation(self, tool, arguments):
        # Operation details are sent privately to the next model, never telemetry.
        self.recovery_message='The repeated operation was '+json.dumps({'tool':tool,'arguments':arguments})+'. Change strategy and save progress; in write-up, write FINAL.tex now.'
        self.early_writeup(self.run.loops.get(fingerprint(tool,arguments),0)>=3)
        if self.run.writeup and not self.report_ready():
            key='minimal_loops' if self.state.get('minimal_report') else 'writeup_loops'
            self.state[key]=self.state.get(key,0)+1
            exhausted=(self.state.get('writeup_loops',0)+self.state.get('minimal_loops',0)>=8) if self.state.get('minimal_report') else self.state[key]>=4
            if exhausted:self.minimal_report('loop-exhausted')
            elif self.state.get('minimal_report'):
                # Coalesce a burst of halt events into one bounded report-only restart.
                self.stop_worker();self.state['worker_retry_at']=self.clock()+60
                self.event('minimal-loop-backoff');self.save()
            else:self.restart('loop-recovery')
            return 'fresh-targeted'
        action=self.run.loop(fingerprint(tool,arguments),self.clock()); self.save()
        if action=='pause-alert': self.pause('loop-exhausted')
        elif action=='fresh-targeted': self.restart('loop-recovery')
        return action
    def compact(self, info):
        if info['compressionStatus']==5: return # Qwen NOOP is not a context failure
        self.state.setdefault('compaction_events',[]).append(dict(info,session=self.state['session'],at=self.clock()))
        if info['compressionStatus']==1:
            self.state['session_depth']={'tokens':max(info.get('originalTokenCount',0),self.state.get('session_depth',{}).get('tokens',0)),'compacted':True}
            self.event('compaction-success'); self.save(); return
        failures=self.state.setdefault('context_failure_sessions',[])
        if self.state['session'] not in failures:failures.append(self.state['session'])
        self.early_writeup()
        if self.run.writeup and not self.report_ready():
            key='minimal_compactions' if self.state.get('minimal_report') else 'writeup_compactions'
            self.state[key]=self.state.get(key,0)+1
            if (not self.state.get('minimal_report') and len(failures)>=2) or self.state.get('writeup_compactions',0)+self.state.get('minimal_compactions',0)>= (3 if self.state.get('minimal_report') else 2):self.minimal_report('compaction-exhausted')
            else:self.restart('compaction-recovery')
            return
        action=compaction_failure(self.state['compaction'],self.state['session'],self.run.lane)
        if type(action) is int: self.run.session_limit=action; self.restart('compaction-fallback')
        elif action=='pause-alert': self.pause('compaction-exhausted')
        elif action=='recover': self.restart('compaction-recovery')
        self.save()
    def goal(self, record):
        from .reply_outcomes import any_capped_session
        if any_capped_session(self.state):
            self.event('capped-goal-rejected');self.save();return
        if 'cause' in record:
            self.state['verifier_feedback'].append(record); self.save()
        if record['status'] not in ('complete','blocked'): return
        # Operator ruling ~13:15 PDT: before the scheduled research stop the first
        # three requests to end early are refused; the fourth is honored.
        stop=getattr(self,'research_stop',None)
        if (stop is not None and self.clock()<stop and not self.run.writeup and not self.state.get('report_lock')
                and self.state.get('early_close_refusals',0)<3):
            n=self.state['early_close_refusals']=self.state.get('early_close_refusals',0)+1
            minutes=max(1,int((stop-self.clock())//60))
            self.event('early-close-refused');self.state['events'][-1].update(count=n,minutes_left=minutes)
            self.save();self.notice('continue-research-%d-%d'%(minutes,n));return
        if self.state.get('error_report'):
            # No third verification: a revised report pair closes the run after one build check.
            if self.report_pair_ready() and self.error_report_revised() and not self.state.get('error_close_pending'):
                self.state['error_close_pending']=True;self.save();self.request_build('error-report-close')
            return
        if self.run.writeup and not self.report_pair_ready():
            self.minimal_report('claim-without-report');return
        if self.run.verify_start is not None: return # verdict is a separate independent check
        if self.state.get('solo_final_check_required') and self.run.cycles >= 1:
            self.state['claimed_status']=record['status']
            self.finish('claimed-final');return
        self.state['claimed_status']=record['status']
        action=self.run.claim(self.clock()); self.save()
        if action=='alert': self.pause('verification-exhausted')
        else: self.restart('fresh-verification')
    def verification_record(self, accepted):
        if (self.run.verify_start is not None and
                self.clock() >= min(self.run.start+self.run.wall, self.run.verify_start+1800)):
            self.verdict(False, 'insufficient-verification-resources');return
        from .reply_outcomes import any_capped_session
        if any_capped_session(self.state):
            self.verdict(False, 'insufficient-verification-resources');return
        identity = self.state.get('verification_identity', {})
        worker = self.worker
        if (not worker or identity.get('session') != self.state['session'] or
                identity.get('model') != getattr(worker, 'model', None) or
                getattr(worker, 'role', None) != 'internal-verifier'):
            self.pause('verifier-identity-mismatch'); return
        from .verification import snapshot
        try:
            current, _ = snapshot(self.verification_source())
        except (ValueError, OSError):
            current = None
        if current != identity.get('candidate_hash'):
            self.verdict(False, 'concrete-actionable-defect'); return
        try:data=json.loads(report_content(Path(identity['output']),identity['record_name'],65536))
        except (ValueError,UnicodeError):data={}
        if not isinstance(data,dict):data={}
        from .contracts_v3 import canonical_json
        try:canonical_json(data)
        except (TypeError,ValueError):data={}
        from .final_checks import evaluate
        inv=identity.get('final_inventory')
        sources=self.path/'retrieved-sources'
        source_bytes={p.name:p.read_bytes() for p in sources.glob('*') if p.is_file() and not p.is_symlink()} if sources.exists() else {}
        result=evaluate(data,inv,receipts=self.state.get('source_receipts',{}),source_bytes=source_bytes) if inv else dict(status='verification_incomplete',findings=[dict(kind='missing-final-inventory')])
        if (self.state.get('final_verification',{}).get('phase') == 'checking' and
                (not accepted or data.get('outcome') not in ('supported-completion','supported-partial'))):
            result['status']='verification_incomplete'
            result['findings'].append(dict(kind='unsupported-verifier-verdict', outcome=data.get('outcome')))
        self.state.setdefault('final_checks',[]).append(dict(session=self.state['session'],report=data,validation=result,
            identity=dict(identity)))
        if self.final_check_result(result):return
        if result['status']!='internal-supported':
            accepted=False
            self.state['verifier_feedback'].append(result)
            self.build_private='Final verification findings: '+json.dumps(result)
        outcome=data.get('outcome')
        allowed=('supported-completion','supported-partial','concrete-actionable-defect','insufficient-verification-resources')
        if outcome not in allowed:outcome='insufficient-verification-resources'
        category=data.get('reason_category')
        categories=('supported','unsupported-claim','invalid-reduction','status-mismatch','placeholder','missing-audit','missing-dependency','feasible-next-step','resources')
        self.state['verification_reason']=category if category in categories else 'resources'
        if not accepted and outcome in ('supported-completion','supported-partial'):
            outcome='concrete-actionable-defect'
            self.state['verification_reason']='unsupported-claim'
        valid=accepted and outcome in ('supported-completion','supported-partial')
        if valid and ((outcome=='supported-completion') != (self.state.get('claimed_status')=='complete')):
            valid=False;outcome='concrete-actionable-defect';self.state['verification_reason']='status-mismatch'
        self.state['verification_attestation'] = dict(identity, outcome=outcome,
            accepted=valid, recorded_at=self.clock())
        self.verdict(valid,outcome)
    def verdict(self, valid, outcome=None):
        if self.state.get('final_verification',{}).get('phase') == 'checking':
            self.final_check_result(dict(status='verification_incomplete', findings=[dict(kind=outcome or 'failed-final-check')]))
            return
        if self.run.verify_start is None: raise ValueError('no verifier')
        outcome=outcome or (('supported-partial' if self.state.get('claimed_status')=='blocked' else 'supported-completion') if valid else 'concrete-actionable-defect')
        if valid:
            from .verification import snapshot
            attestation = self.state.get('verification_attestation', {})
            try: digest, _ = snapshot(self.work)
            except (ValueError, OSError): digest = None
            if (not attestation.get('accepted') or attestation.get('cycle') != self.run.cycles
                    or digest != attestation.get('candidate_hash')):
                valid = False
                outcome = 'insufficient-verification-resources'
            report_hash=self.state.get('verification_identity',{}).get('report_candidate_hash')
            if report_hash in self.state.get('rejected_report_candidates',[]):
                valid=False;outcome='concrete-actionable-defect'
        if valid:
            self.report_status()
            if self.state['report_delivery']!='delivered':
                if not self.report_pair_ready():valid=False;outcome='concrete-actionable-defect'
                elif self.state.pop('verification_build_checked',False):valid=False;outcome='concrete-actionable-defect'
                else:
                    self.state['pending_verdict']=outcome
                    self.stop_worker()
                    self.request_build('verification');return
        if not valid:
            self.state.pop('pending_verdict',None)
            self.state.pop('verification_build_checked',None)
        self.state['verification_outcome']=outcome
        self.state['internal_status'] = ('internal-supported' if valid else 'indeterminate'
            if outcome == 'insufficient-verification-resources' else 'internal-rejected')
        self.state['internal_candidate_hash'] = self.state.get('verification_identity', {}).get('candidate_hash')
        report_hash=self.state.get('verification_identity',{}).get('report_candidate_hash')
        if self.state['internal_status']=='internal-rejected' and report_hash:
            rejected=self.state.setdefault('rejected_report_candidates',[])
            if report_hash not in rejected:rejected.append(report_hash)
        if self.closure is not None:
            candidate=self.state.get('verification_identity',{}).get('report_candidate_hash')
            if candidate:
                decision={'status':self.state['internal_status'],
                    'record_id':f"verifier-{self.state['session']}-cycle-{self.run.cycles}"}
                self.state.setdefault('closure_statuses',{})[candidate]=decision
                bundle=self.state.get('closure_bundles',{}).get(candidate)
                if bundle or decision['status']=='internal-rejected':
                    self.closure.submit('status',bundle_id=bundle,candidate_hash=candidate,**decision)
                self.save();self.closure.dispatch()
        self.run.verdict(valid); self.save()
        if valid:self.finish('verified-blocked' if outcome=='supported-partial' else 'verified-completion')
        elif self.run.phase=='paused-alert':self.error_report()
        else:self.restart('verification-rejected')
    def enter_error_report(self):
        """Verification is exhausted: rewrite the report around the rejected claim instead of pausing."""
        import hashlib
        self.state['error_report']=True
        self.state['error_report_digest']=hashlib.sha256(report_content(self.work,'FINAL.tex')).hexdigest()
        self.state['writeup_kind']='write-up-rejected';self.state['notice']=None
        self.state.pop('writeup_delivered_at',None)
        self.run.writeup=True;self.run.audit=False;self.run.phase='write-up'
        self.run.verify_start=None;self.run.recovery_start=None
        self.run.notice_due=self.clock();self.run.notice_delivered=False
        self.enter_report_mode('verification-exhausted')
    def error_report(self):
        self.enter_error_report();self.event('error-report');self.restart('error-report')
    def error_report_revised(self):
        import hashlib
        return hashlib.sha256(report_content(self.work,'FINAL.tex')).hexdigest()!=self.state.get('error_report_digest')
    def score_notices(self):
        score=DepthScore(self.state.setdefault('depth_scores',{}))
        for observation in self.state.get('notice_observations',{}).values():
            if observation['kind'].startswith('consolidate') or observation['kind'] in ('audit-polish','build-failure'):
                observation['score']='consolidation-separate' if observation['kind'].startswith('consolidate') else observation['kind']+'-separate';continue
            if observation.get('score'): continue
            now=self.clock()
            previous=observation.get('last_observed_at',observation['at'])
            if not self.busy():
                observation['eligible_seconds']=observation.get('eligible_seconds',0)+max(0,now-previous)
            observation['last_observed_at']=now
            age=observation.get('eligible_seconds',0)
            if self.run.outage_start is not None:observation['outage']=True
            success=all(observation[k] for k in ('attached','acknowledged','written'))
            if not success and age<300:continue
            if observation['outage']:
                observation['score']='provider-unavailable';continue
            if observation['kind'] not in ('checkpoint','session-wall') and not observation['kind'].startswith('write-up'):
                # Unscored kinds (e.g. continue-research): solo check 12 crashed here.
                observation['score']='not-scored';continue
            observation['score']=score.observe('write-up' if observation['kind'].startswith('write-up') else observation['kind'],depth=observation['depth'],compacted=observation['compacted'],attached=observation['attached'],acknowledged=observation['acknowledged'],written=observation['written'],seconds=age)
    def tick(self, boundary=True, external_key_delta=0, whole_key_limit=None):
        self.runtime_durable()
        if self.state.get('runtime_durability_failure'):
            self.run.phase='done';self.stop_worker()
            if not self.state.get('terminal'):
                self.state['terminal']=dict(reason='runtime-durability-failure',spent=self.ledger.spent,
                    budget=self.ledger.cap,at=self.clock(),artifact_closure='unconfirmed')
                self.save()
            return 'durability-failure'
        self.closure_poll()
        if self.state['terminal']: return 'done'
        if self.state.get('receipt_failure'):
            self.finish('reply-consumption-failure');return 'receipt-failure'
        self.consume()
        if self.state['terminal']: return 'done'
        from .reply_outcomes import any_capped_session
        capped=any_capped_session(self.state)
        if (capped and self.worker is not None and not self.state.get('pending') and
                getattr(self.worker, 'boundary', True)):
            if self.run.verify_start is not None:
                self.verdict(False, 'insufficient-verification-resources');return 'failed-verification-attempt'
            if getattr(self.worker, 'role', None) == 'report-author':
                self.finish('output-cap-author');return 'failed-author-attempt'
        if self.state.get('solo_final_check_required'):
            final=self.state.get('final_verification')
            if final:
                self.finish(final['reason']);return 'final-check' if final['phase']!='done' else 'done'
            # The reserved final-check window was a fixed 30 min, which in a 1-hour
            # solo check began before research even stopped (fake solo check).
            if self.clock() >= self.run.start+self.run.wall-min(1800,self.run.wall//8):
                self.finish('reserved-final-check-window');return 'final-check'
        if self.state.get('admission_stop'): self.finish(self.state['admission_stop']); return 'stop'
        if self.state.get('depth_restart_due') and self.worker is not None and not self.busy():
            self.state.pop('depth_restart_due',None);self.restart('depth-delivery-fallback')
        if self.state.get('closing'): self.finish(self.state['closing']); return 'done'
        if whole_key_limit is not None and external_key_delta>=whole_key_limit:
            self.finish('whole-key-backstop'); return 'stop' # only this owned worker
        in_flight=bool(self.state.get('pending')) and self.worker is not None and not getattr(self.worker,'boundary',True)
        # Settled spend at the budget stops research by entering report mode; the
        # author and final check always run (operator ruling 2026-10-05).
        if self.ledger.spent>=__import__('v2.budget',fromlist=['x']).research_limit(self.ledger.cap) and not self.state.get('report_lock') and not self.run.writeup and hasattr(self,'enter_report_mode'):
            self.enter_report_mode('research-pool-limit')
        if self.clock()>=self.run.start+self.run.wall:self.finish('deadline');return 'stop'
        self.early_writeup(self.run.recovery_start is not None and self.clock()-self.run.recovery_start>=600)
        if self.run.verify_start is not None and self.clock() >= self.run.verify_start+1800:
            self.verdict(False, 'insufficient-verification-resources')
            return 'verification-timeout'
        if self.worker is not None and not self.worker.alive():
            evidence = self.worker.exit_evidence() if hasattr(self.worker, 'exit_evidence') else {}
            self.worker_exited(evidence)
        if self.worker is None and self.run.phase != 'paused-alert':
            if self.state.get('worker_restart_pending') and not self.state.get('pending'):
                self.start()
            if self.run.outage_start is not None:
                if self.state['retry_at'] is not None and self.clock() >= self.state['retry_at']:
                    # Reopen for one serialized provider attempt; another failure restores backoff.
                    self.run.healthy(self.clock()); self.state['retry_at']=None; self.start()
            elif self.state.get('worker_retry_at') is not None and self.clock() >= self.state['worker_retry_at']:
                self.start()
        if self.worker is not None:
            self.promote_report(self.worker)
            # Coordinator ~16:35 PDT: during write-up, compile the changed draft every
            # 3 minutes; a failure reaches the same author as a build-failure notice,
            # and every passing build is a model bundle that can be selected at freeze.
            draft=self.work/'FINAL.tex'
            if (self.run.writeup and self.closure is not None and not self.state.get('build_pending')
                    and draft.exists() and draft.stat().st_size>1000
                    and self.clock()>=self.state.get('progress_build_at',0)+180
                    and draft.stat().st_mtime>self.state.get('progress_build_mtime',0)):
                self.state['progress_build_at']=self.clock();self.state['progress_build_mtime']=draft.stat().st_mtime
                self.request_build('writeup-progress')
            after=scan(self.work); changes=changed(self.state['snapshots'],after)
            if changes: self.run.write(self.clock(),bool(changes & ({'NOTES.md','EXPERIMENTS.md','FINAL.tex','AUDIT.md'} if self.run.writeup else {'NOTES.md','EXPERIMENTS.md'})))
            self.state['snapshots']=after
            for observation in self.state.get('notice_observations',{}).values():
                if observation.get('score') or observation['session']!=self.state['session']: continue
                observation['files']=sorted(set(observation['files'])|changes)
                required={'FINAL.tex','AUDIT.md'} if observation['kind'].startswith('write-up') else {'NOTES.md','EXPERIMENTS.md'}
                observation['written']=(required<=set(observation['files']) if observation['kind'].startswith('write-up') else bool(required & set(observation['files'])))
                if observation['written'] and 'written_at' not in observation: observation['written_at']=self.clock()
        # Select sticky write-up before testing its 80% consolidation exception.
        if not self.run.writeup and (self.clock()>=self.run.start+(self.run.writeup_at if self.run.writeup_at is not None else .9*self.run.wall) or self.ledger.spent>=.9*self.ledger.cap):
            self.enter_report_mode('wall-or-budget')
        # Restored v2 write-up is upgraded to the same irreversible lock.
        if self.run.writeup and not self.state.get('report_lock'):
            self.enter_report_mode('restored-write-up')
        self.report_handover()
        self.build_watch()
        if self.state['terminal']:return 'done'
        self.score_notices()
        consolidating=False
        if self.run.phase!='paused-alert' and self.run.outage_start is None:
            consolidating=not self.state.get('report_lock') and consolidate(self,boundary)
            if not consolidating or self.state.get('minimal_report'):self.writeup_watch()
        if self.state['terminal']:return 'done'
        if self.state.get('worker_retry_at') is not None and self.clock() < self.state['worker_retry_at'] and self.clock() < self.run.start+self.run.wall:
            self.save(); return 'worker-backoff'
        action=self.run.tick(self.clock(),self.ledger.spent,boundary)
        if consolidating and action in ('deliver-writeup','restart-for-writeup','checkpoint','checkpoint-grace','checkpoint-wall','restart-checkpoint','loop-recovery-exhausted'):
            action='consolidate'
        self.save()
        if action=='stop': self.finish('deadline' if self.clock()>=self.run.start+self.run.wall else 'budget')
        elif action=='restart-for-writeup': self.missing_notice()
        elif action in ('restart-checkpoint','restart-wall') and self.busy(): pass
        elif action.startswith('restart-'): self.restart(action)
        elif action=='stop-wall-during-outage': self.stop_worker(); self.run.wall_cleanup_ack(); self.save()
        elif action in ('deliver-writeup','checkpoint','checkpoint-grace','checkpoint-wall'):
            kind=('audit-polish' if self.run.audit else self.state.get('writeup_kind','write-up')) if action=='deliver-writeup' else 'session-wall' if action=='checkpoint-wall' else 'checkpoint-final' if action=='checkpoint-grace' else 'checkpoint'
            self.notice(kind)
        elif action=='verification-timeout': self.verdict(False,'insufficient-verification-resources')
        elif action=='loop-recovery-exhausted' and self.run.writeup and not self.report_ready():self.minimal_report(action)
        elif action in ('billing-exhausted','loop-recovery-exhausted'): self.pause(action)
        notice=self.state['notice']
        if self.worker is not None and self.run.outage_start is None and notice and not notice['kind'].startswith('consolidate') and not notice['attached'] and self.clock()-notice['at']>=240:
            self.missing_notice()
        return action
