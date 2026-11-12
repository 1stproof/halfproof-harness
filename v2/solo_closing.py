"""Compulsory solo closure, using the existing two-cycle verification allowance."""
from pathlib import Path


class SoloClosing:
    def verification_source(self):
        final = self.state.get('final_verification', {})
        return Path(final['source']) if final.get('source') else self.work

    def final_check_gate(self, reason):
        """Return True only when selection can freeze. Called by every finish path."""
        if not self.state.get('solo_final_check_required'):return True
        final = self.state.setdefault('final_verification', dict(reason=reason, phase='prepare'))
        if final['phase'] == 'done':return True
        now = self.clock()
        deadline = self.run.start + self.run.wall
        if final['phase'] == 'checking':
            if (now >= min(deadline, final['started_at']+1800) or
                    self.state.get('admission_stop') or self.state.get('receipt_failure') or
                    self.worker is None or not self.worker.alive()):
                self.complete_final_check('verification_incomplete', 'check-unavailable-or-expired')
                return True
            return False
        # Fence the author before building/selecting immutable final bytes.
        self.enter_report_mode('compulsory-final-check')
        self.stop_worker()
        self.state['report_handover_pending'] = False
        self.run.outage_start = None
        self.run.verify_start = None
        self.run.phase = 'write-up'
        self.state.pop('pending_verdict', None)
        if self.state.get('pending'):return False
        if now >= deadline or self.state.get('receipt_failure'):
            self.complete_final_check('verification_incomplete', 'original-deadline-or-receipt-failure')
            return True
        if self.closure is not None:
            if not self.state.get('closure_ready'):return False
            if not final.get('build_requested'):
                final['build_requested'] = True
                self.save()
                self.report_status()
                if self.report_pair_ready() and self.state['report_delivery'] != 'delivered':
                    self.request_build('compulsory-final-check')
            if self.state.get('build_pending') or self.closure.state['pending']:return False
            if not final.get('selected'):
                if not final.get('selection_id'):
                    final['selection_id'] = self.closure.submit('select-final')
                    self.save();self.closure.dispatch()
                return False
            selected = final['selected']
            bundle = Path(self.closure.catalog['artifact_root'])/'bundles'/selected['bundle_id']
            previous = (self.state.get('final_checks') or [{}])[-1]
            import hashlib
            if (self.state.get('verification_attestation', {}).get('accepted') and
                    previous.get('identity', {}).get('report_candidate_hash') == selected['candidate_sha256'] and
                    previous.get('validation', {}).get('status') == 'internal-supported' and
                    previous['validation'].get('rendered_sha256') == hashlib.sha256((bundle/'FINAL.pdf').read_bytes()).hexdigest()):
                final['validation'] = previous['validation']
                self.complete_final_check('internal-supported', 'existing-exact-document-check')
                return True
            if not final.get('source'):
                # Preserve problem/source evidence while replacing the candidate
                # with the exact selected compiled bundle (possibly fallback).
                from .verification import snapshot
                from .closure_io import read_file
                source = self.path/'final-verification-evidence'
                source.mkdir(exist_ok=True)
                try:
                    evidence = source/'AUTHOR-EVIDENCE'
                    evidence.mkdir(exist_ok=True)
                    snapshot(self.work, evidence)
                    if (evidence/'PROBLEM.md').is_file():
                        (source/'PROBLEM.md').write_bytes(read_file(evidence, 'PROBLEM.md'))
                        (source/'PROBLEM.md').chmod(0o444)
                    for name in selected['files']:
                        target = source/name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():target.unlink()
                        target.write_bytes(read_file(bundle, name));target.chmod(0o444)
                    from .runtime_closure import candidate_hash
                    if candidate_hash(source) != selected['candidate_sha256']:
                        raise ValueError('selected report dependency closure changed')
                    final['source'] = str(source)
                except (OSError, ValueError):
                    self.complete_final_check('verification_incomplete', 'final-snapshot-unavailable')
                    return True
        else:
            # Standalone fake-provider controllers use their bounded snapshot.
            final['source'] = str(self.work)
        if self.run.cycles >= 2:
            self.complete_final_check('verification_incomplete', 'verification-cycle-limit')
            return True
        self.state.pop('admission_stop', None)  # author pool exhaustion does not spend verifier escrow
        self.state.pop('worker_retry_at', None)
        self.state.pop('worker_restart_pending', None)
        final.update(phase='checking', started_at=now)
        self.run.claim(now)
        if self.state.get('claimed_status') is None:self.state['claimed_status'] = 'blocked'
        self.save();self.start()
        return final['phase'] == 'done'

    def complete_final_check(self, status, cause=None):
        final = self.state['final_verification']
        final.update(phase='done', status=status, completed_at=self.clock(), cause=cause)
        self.run.verify_start = None
        self.state['verification_incomplete'] = status != 'internal-supported'
        self.state['internal_status'] = status
        self.save()

    def final_check_result(self, result):
        final = self.state.get('final_verification', {})
        if final.get('phase') != 'checking':return False
        status = result['status']
        final['validation'] = result
        self.complete_final_check(status)
        selected = final.get('selected')
        if selected and selected['authorship'] == 'model' and self.closure is not None:
            # Incomplete coverage is not a positive correctness verdict.
            self.closure.submit('status', bundle_id=selected['bundle_id'],
                candidate_hash=selected['candidate_sha256'],
                status='internal-supported' if status == 'internal-supported' else 'indeterminate',
                record_id=f"final-verifier-{self.state['session']}-cycle-{self.run.cycles}")
            self.save();self.closure.dispatch()
        self.finish(final['reason'])
        return True
