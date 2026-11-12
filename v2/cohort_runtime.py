"""Bounded fake-provider cohort integration with a complete registered roster.

The explicit fake-provider scope may include solos and swarms. Unselected arms
remain registered and unstarted, preserving the cohort judging embargo. No paid entry,
credential discovery, automatic withdrawal, or runtime judging adapter exists.
"""
import hashlib
import json
from pathlib import Path
import time

from .accounting import AccountingStore
from .accounting_profiles import configure_cohort, preflight_author_reserves
from .contracts_v3 import load_contracts, canonical_json
from .control import atomic_json
from .deployment import local_root, DeploymentRefused
from .durability import Publisher
from .solo_runtime import SoloRuntime
from .pacing import advance, fixture_rows


class CohortRuntime:
    def __init__(self, root, *, source, release, durable_root, contracts_sha256,
                 release_sha256, cohort, problem, problem_text, fake_providers,
                 prices, route_layouts, withdrawal_authority, withdrawal_deadline,
                 profile='pilot', clock=time.time, fixture_hashes=None,
                 live_transport=None, problem_mount=None, live_arms=None):
        self.root = local_root(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root/'cohort-controller.json').exists():
            raise DeploymentRefused('existing controller requires qualified restore; fresh launch cannot renew clocks')
        self.source, self.release = Path(source), Path(release)
        self.durable = Path(durable_root)
        self.clock, self.profile = clock, profile
        self.policy = load_contracts(expected_sha256=contracts_sha256)
        self.live=live_transport is not None
        self.transport_key=None
        self.credential_id='fake'
        if self.live:
            if fake_providers is not None or profile not in ('pilot','production','swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue') or fixture_hashes or not problem_mount:
                raise DeploymentRefused('live entry requires a declared real-problem profile')
            if (not callable(live_transport.get('load_key')) or
                    live_transport.get('prices_verified') is not True or
                    live_transport.get('metadata_lookup_free') is not True):
                raise DeploymentRefused('qualified live pricing/metadata and deferred credential required')
            if profile in ('production','swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue'):
                if clock is not time.time:
                    raise DeploymentRefused('live production requires the real wall clock')
                from .production import validate_problem, require_current_profile
                if profile == 'swarm-check':from .swarm_check import require_current_profile
                if profile == 'solo-check':from .solo_check import require_current_profile
                if profile == 'closing-check':from .closing_check import require_current_profile
                if profile == 'prod-check':from .prod_check import require_current_profile
                if profile == 'open-check':from .open_check import require_current_profile
                if profile == 'open-production':from .open_production import require_current_profile
                if profile == 'prod-continue':from .prod_continue import require_current_profile
                if profile == 'open-continue':
                    from .open_production import require_current_profile as _r
                    require_current_profile=lambda policy:_r(policy,'open-continue')
                validate_problem(problem_mount)
            else:
                from .pilot import problem_record, require_current_profile
                if problem_mount!=problem_record():raise DeploymentRefused('real pilot input changed')
            require_current_profile(load_contracts(expected_sha256=contracts_sha256, apply_continuation=False))
            # A declared subset is started; the rest stay registered but blocked.
            selected=sorted(self.policy[profile]) if live_arms is None else sorted(live_arms)
            if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(self.policy[profile]):
                raise DeploymentRefused('declared live arm subset required')
            fake_providers={arm:None for arm in selected}
            self.credential_id=live_transport['credential_id']
        if not fake_providers or not set(fake_providers) <= set(self.policy[profile]):
            raise DeploymentRefused('explicit declared fake-provider scope required')
        if not self.live and any(not callable(getattr(provider, 'stream', None)) for provider in fake_providers.values()):
            raise DeploymentRefused('bounded synthetic stream provider required')
        if not withdrawal_authority or withdrawal_deadline <= clock():
            raise DeploymentRefused('original operator withdrawal decision deadline required')
        # The sole live credential source is the deferred, post-readiness loader.
        # Prices also bound every fake request in the offline composition.
        self.streams, self.prices, self.layouts = fake_providers, prices, route_layouts
        self.contracts_sha256, self.cohort, self.problem = contracts_sha256, cohort, problem
        self.problem_text = problem_text
        self.runtimes, self.started, self.fences, self.frozen = {}, set(), {}, {}
        self.operations = {}
        self.failed = None
        self.handoff = False
        self.events = []
        self.deployment = {'mode':('production' if profile in ('swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue') else profile) if self.live else 'test', 'fixtures':[]}
        if problem_mount:self.deployment['problem']=problem_mount
        if fixture_hashes:
            if profile != 'pilot':raise DeploymentRefused('production refuses synthetic pacing')
            self.deployment = {'mode':'pilot', 'fixtures':fixture_rows(self.source,fixture_hashes)}
            from .deployment import fixture_mounts
            fixture_mounts(self.deployment['fixtures'], mode='pilot')
            self.events = json.loads((self.source/'tests/p8/fixture/events.json').read_text())
        from .startup_log import phase
        phase('publisher start')
        self.publisher = Publisher(self.root/'publisher', self.durable/'journal')
        phase('publisher ready; pre-clock readiness start')
        try:
            from .cohort_readiness import prepare
            self.readiness = prepare(self)
            phase('pre-clock readiness done')
            from .production_continuation import active
            continuation = active() if profile == 'production' else None
            self.start = continuation['prepared_at'] if continuation else clock()
            if continuation and clock() >= continuation['deadline']-self.policy[profile]['mixed-swarm']['closing_max_seconds']:
                raise DeploymentRefused('continuation research deadline expired during setup')
            self.deadline = self.start + max(row['wall_seconds'] for row in self.policy[profile].values())
            self.cleanup_deadline = self.deadline + 420
            if withdrawal_deadline < self.cleanup_deadline:
                raise DeploymentRefused('withdrawal review cannot precede bounded cohort cleanup')
            self.store = AccountingStore(self.root/'accounting.sqlite', clock=clock)
            configure_cohort(self.store, cohort=cohort, problem=problem, profile=profile,
                starts={arm:self.start for arm in self.policy[profile]},
                expected_contracts_sha256=contracts_sha256, route_layouts=route_layouts)
            preflight_author_reserves(self.store, prices=prices)
            if self.live:
                self.transport_key=live_transport['load_key']()
                if not isinstance(self.transport_key,str) or not self.transport_key.strip():
                    raise DeploymentRefused('empty live credential')
            if self.live:
                from .pilot_ramp import configure
                configure(self.store,self.policy,self.start)
            arm_hashes = {arm:hashlib.sha256(canonical_json(dict(strategy=arm,profile=profile,
                cap=row['cap_microusd'],start=self.start,deadline=self.start+row['wall_seconds'],
                contracts_sha256=contracts_sha256,release_sha256=release_sha256))).hexdigest()
                for arm,row in self.policy[profile].items()}
            self.state = dict(schema=1, cohort=cohort, problem=problem, profile=profile,
                readiness=self.readiness, live=self.live, original_start=self.start, deadline=self.deadline, cleanup_deadline=self.cleanup_deadline,
                pilot_started_at=self.start, pacing_cursor=0, scientific_events=[], sequence=0,
                release_sha256=release_sha256, contracts_sha256=contracts_sha256,
                registered_arms=sorted(self.policy[profile]), started_arms=[], frozen_arms={},
                blocked_arms={arm:'not selected for this launch' if self.live else 'not selected for fake-provider execution' for arm in self.policy[profile] if arm not in self.streams})
            self._submit('register', 'cohort-register', registration=dict(
                cohort_id=cohort,problem_id=problem,problem_text=problem_text,arm_ids=sorted(self.policy[profile]),
                withdrawal_authority=withdrawal_authority,withdrawal_deadline=withdrawal_deadline,
                release_sha256=release_sha256,arm_manifest_sha256s=arm_hashes,
                registered_at=self.start,latest_start_at={arm:self.start for arm in self.policy[profile]}),now=self.start)
            self.checkpoint()
        except BaseException:
            self.publisher.close()
            raise

    def _submit(self, identity, operation, **payload):
        value = dict(root=str(self.root/'registry'),artifact_root=str(self.durable/'cohort'),**payload)
        self.operations[identity] = dict(operation=operation,payload=value)
        atomic_json(self.root/'cohort-operations.json', self.operations)
        self.publisher.submit_operation(identity,operation,value)

    def checkpoint(self):
        from .pause import RunClock
        if isinstance(self.clock, RunClock):self.state['operator_clock'] = self.clock.snapshot()
        self.state['sequence'] += 1
        identity = 'cohort-checkpoint-'+str(self.state['sequence'])
        value = dict(controller=self.state, accounting=self.store.checkpoint())
        atomic_json(self.root/'cohort-controller.json', value)
        self.publisher.submit(identity,value)
        return identity

    def _make_runtimes(self):
        for arm,stream in sorted(self.streams.items()):
            if arm.endswith('-swarm'):
                if arm in getattr(self,'_restored_swarms',{}):
                    from .swarm_restore import restore as restore_swarm
                    runtime=restore_swarm(self,arm,self._restored_swarms[arm])
                else:
                    from .campaign_worker import swarm_parent
                    runtime=swarm_parent(dict(strategy=arm,replica=1),root=self.root/'arms',profile=self.profile,
                        cohort=self.cohort,problem=self.problem,start=self.start,
                        contracts_sha256=self.contracts_sha256,release=self.release,accounting=self.store,
                        publisher=self.publisher,durable_root=self.durable,prices=self.prices,
                        fake=stream,clock=self.clock,deployment=self.deployment,
                        **(dict(key=self.transport_key,credential_id=self.credential_id,
                            prices_verified=True,metadata_lookup_free=True) if self.live else {}))
                problem_path=runtime.watchdog.work/'PROBLEM.md'
                if problem_path.exists():
                    if problem_path.is_symlink() or problem_path.read_text()!=self.problem_text:
                        raise DeploymentRefused('retained problem input changed')
                else:problem_path.write_text(self.problem_text)
                self.runtimes[arm]=runtime
                self._submit('start-'+arm,'cohort-start',arm=arm,started_at=self.start)
                continue
            model = self.policy['models'][self.policy[self.profile][arm]['models'][0]]['model_id']
            ip,op = self.prices[model]
            runtime = SoloRuntime(self.root/'arms',strategy=arm,profile=self.profile,
                cohort=self.cohort,problem=self.problem,start=self.start,
                contracts_sha256=self.contracts_sha256,release=self.release,accounting=self.store,
                publisher=self.publisher,durable_root=self.durable,input_price=ip,output_price=op,
                fake=stream,clock=self.clock,continuous_goal=self.live,deployment=self.deployment,
                key=self.transport_key,credential_id=self.credential_id if self.live else 'fake:'+arm,
                prices_verified=self.live,metadata_lookup_free=self.live)
            problem_path=runtime.watchdog.work/'PROBLEM.md'
            if problem_path.exists():
                if problem_path.is_symlink() or problem_path.read_text()!=self.problem_text:
                    raise DeploymentRefused('retained problem input changed')
            else:problem_path.write_text(self.problem_text)
            self.runtimes[arm] = runtime
            self._submit('start-'+arm,'cohort-start',arm=arm,started_at=self.start)

    def _advance_pacing(self):
        if not self.events:return
        def transition(state,event):
            state['scientific_events'].append(event)
        due = advance(self.state,self.events,self.clock(),transition,mode='test')
        if not due:return
        # FIFO publisher ordering puts this original-clock cursor before any new
        # per-request checkpoint acknowledgment; in-flight requests keep bounds.
        self.checkpoint()
        for runtime in self.runtimes.values():
            if runtime.watchdog.state.get('terminal'):continue
            atomic_json(runtime.watchdog.work/'PILOT-EVENTS.json', self.state['scientific_events'])
        if 'research-stop' in due:
            for runtime in self.runtimes.values():
                with runtime.watchdog.mutex:
                    if not runtime.watchdog.state.get('terminal'):
                        runtime.watchdog.enter_report_mode('original-pilot-research-stop')
        if 'authoring-stop' in due:self.close('original-pilot-authoring-stop')

    def _freeze(self, arm, runtime):
        w = runtime.watchdog
        if hasattr(runtime,'freeze_ready') and not runtime.freeze_ready():return
        if arm not in self.fences:
            if not w.runtime_durable():return
            from .fencing import FencePending
            try:self.fences[arm] = runtime.fence_receipt()
            except FencePending:return
            self.checkpoint()
            return
        if not w.runtime_durable():return
        operation = 'freeze-'+arm
        if operation not in self.operations:
            self.store.assert_conservation()
            ledger_id = 'terminal-ledger-'+arm
            self.publisher.submit(ledger_id,self.store.export())
            fence = self.fences[arm]
            supporting = []
            extra=runtime.scientific_sections() if hasattr(runtime,'scientific_sections') else {}
            supporting.extend(extra.get('supporting',[]))
            for session in sorted(w.path.glob('session-*')):
                for name in ('role-output','evidence','qwen-state'):
                    path = session/name
                    if path.is_dir():
                        supporting.append(dict(root=str(path),prefix=session.name+'/'+name+'/',
                                               transcripts_only=name=='qwen-state'))
            self._submit(operation,'cohort-freeze',arm=arm,frozen_at=self.clock(),
                fence=dict(arm_id=arm,problem_id=self.problem,admission_closed=True,pending_requests=0,
                    active_writers=0,namespaces_fenced=True,admission_receipt=ledger_id,
                    writer_receipt=fence['sha256'],cancellation_records=fence['cancelled_request_ids']),
                sections=dict(reports=[dict(root=str(self.durable/arm/'closure'),durable_report=True)],
                    drafts=[dict(root=str(w.work))],board=extra.get('board',[]),supporting=supporting),
                closure_manifest=w.state['closure_manifest'],ledger_export_id=ledger_id,
                board_versions=runtime.scientific_versions() if hasattr(runtime,'scientific_versions') else [])
        result = self.publisher.operation_result(operation)
        if result is not None:
            self.frozen[arm] = result
            self.state['frozen_arms'][arm] = result['snapshot_id']
            self.checkpoint()

    def step(self):
        if getattr(self, 'handoff', False):
            raise DeploymentRefused('controller ownership transferred; restore required')
        if self.failed:raise DeploymentRefused(self.failed)
        self.publisher.poll()
        if self.live:
            from .pilot_ramp import advance as advance_ramp
            advance_ramp(self.store)
        if self.publisher.operation_result('register') is None:return
        if not self.runtimes:self._make_runtimes()
        self._advance_pacing()
        for arm,runtime in self.runtimes.items():
            if arm in self.frozen:continue
            if arm not in self.started:
                if self.publisher.operation_result('start-'+arm) is None:continue
                self.started.add(arm)
                self.state['started_arms'] = sorted(self.started)
                self.checkpoint()
                runtime.start()
            runtime.step()
            w = runtime.watchdog
            if w.state.get('receipt_failure') or w.state.get('runtime_durability_failure'):
                raise DeploymentRefused('runtime receipt/durability failure')
            if w.state.get('terminal') and w.state.get('closure_manifest'):
                self._freeze(arm,runtime)
        if self.clock() >= self.deadline:self.close('original-cohort-deadline')
        if self.clock() >= self.cleanup_deadline and not self.complete:
            raise DeploymentRefused('original cohort cleanup deadline exceeded')
        return dict(started=sorted(self.started),frozen=sorted(self.frozen),
                    registered=self.state['registered_arms'],
                    judging_eligible=set(self.frozen)==set(self.state['registered_arms']))

    @property
    def complete(self):
        return bool(self.runtimes) and set(self.runtimes)==set(self.frozen)

    def close(self, reason='controller-stop'):
        for arm,runtime in self.runtimes.items():
            if not runtime.watchdog.state.get('terminal') and not runtime.watchdog.state.get('closing'):
                runtime.close(reason)

    def shutdown(self):
        """Emergency local reap preserves liabilities; never manufactures freeze."""
        if getattr(self, 'handoff', False):
            if not getattr(self,'cancelled',False):return
            # A paused cancellation owns the sealed state. No scheduler/services
            # restart; salvage the retained transcripts and accounting directly.
            self.store.block('paused-controller-cancelled')
            result=self.cancel_salvage(seconds=min(180,max(0,self.cleanup_deadline-self.clock())))
            if result.get('durability_failed'):raise DeploymentRefused('paused cancellation salvage not durable')
            self.handoff=False
            return
        self.store.block('controller-shutdown')
        # One shared 330-second reconciliation budget, inside one 420-second
        # cleanup window. Future-due lookups never keep scientific work alive.
        cleanup_end=min(time.monotonic()+420,time.monotonic()+max(0,self.cleanup_deadline-self.clock()))
        reconcile_end=min(time.monotonic()+330,cleanup_end)
        errors=[]
        if not self.complete or getattr(self,'cancelled',False):
            from .deadline_delivery import release_runtime
            for arm,runtime in self.runtimes.items():
                try:
                    release_runtime(runtime,self.durable,self.problem.removeprefix('b3-'),
                                    reason='controller stopped before checks completed')
                except Exception as exc:errors.append('draft delivery: '+str(exc))
        for runtime in self.runtimes.values():
            w=runtime.watchdog
            try:
                with w.mutex:
                    if w.worker:w.stop_worker()
            except Exception as exc:
                errors.append(str(exc))
                # A failed journal must not prevent physical emergency reap.
                # Keep the failure/unknown liability; do not manufacture a fence.
                if w.worker is not None:
                    try:w.worker.stop()
                    except Exception as stop_error:errors.append(str(stop_error))
            for gateway in runtime.gateways:
                try:gateway.cancel_worker(None)
                except Exception as exc:errors.append(str(exc))
            if hasattr(runtime,'stop_services'):
                try:runtime.stop_services()
                except Exception as exc:errors.append(str(exc))
        if not self.complete or getattr(self,'cancelled',False):
            # Operator 2026-10-07: a cancelled job that froze cleanly still salvages (transcripts,
            # final settlement inside the job, cancel-salvage.json); before, a clean freeze skipped it.
            salvage=self.cancel_salvage(seconds=max(0,min(180,cleanup_end-time.monotonic()-30)))
            if salvage.get('durability_failed'):
                errors.append('cancel settlement not durable: '+str(salvage['checkpoint'].get('direct_file_error')))
        if self.complete and not errors:
            try:
                for runtime in self.runtimes.values():
                    for gateway in runtime.gateways:
                        gateway.wait_reconciliation(seconds=max(0,reconcile_end-time.monotonic()))
                if any(g.reconciliation_thread and g.reconciliation_thread.is_alive()
                       for r in self.runtimes.values() for g in r.gateways):
                    raise DeploymentRefused('metadata cleanup remains active')
                self.store.assert_conservation()
                final=self.checkpoint()
                until=min(cleanup_end,time.monotonic()+60)
                while not self.publisher.require_ack(final):
                    if time.monotonic()>=until:raise DeploymentRefused('final checkpoint durability timeout')
                    time.sleep(.01)
            except Exception as exc:errors.append(str(exc))
        reaped=self.publisher.close()
        if errors or not reaped:raise DeploymentRefused('bounded cleanup failed: '+str(errors))

    def cancel_salvage(self, *, seconds=180):
        """Cancel/SIGTERM: best-effort transcript copy into durable storage (<=60 s), then one
        final settlement pass of held liability by generation lookups. Never raises."""
        from . import cancel_salvage as cs
        until = time.monotonic() + seconds
        report = dict(at=self.clock(), transcripts={}, settlement=None)
        try:
            for arm, runtime in self.runtimes.items():
                path = getattr(runtime, 'path', None) or getattr(runtime, 'root', None)
                left = min(60, until - time.monotonic()) / max(1, len(self.runtimes))
                if path is None or left <= 0:continue
                # Daemon thread joined with the bound: stalled filesystem I/O never blocks shutdown.
                import threading as _th
                box = {}
                def _copy(path=path, arm=arm, left=left, box=box):
                    try:box['r'] = cs.save_transcripts(path, self.durable/'cancel-transcripts'/arm, seconds=left)
                    except Exception as exc:box['r'] = dict(error=str(exc)[:200])
                try:
                    t = _th.Thread(target=_copy, daemon=True); t.start(); t.join(left + 5)
                    report['transcripts'][arm] = box.get('r', dict(error='transcript copy timed out'))
                except Exception as exc:report['transcripts'][arm] = dict(error=str(exc)[:200])
            gateway = next((g for r in self.runtimes.values() for g in getattr(r, 'gateways', []) if getattr(g, 'key', None) or getattr(g, 'fake', None)), None)
            if gateway is not None and until - time.monotonic() > 0:
                for g in (g for r in self.runtimes.values() for g in getattr(r, 'gateways', [])):
                    try:g.wait_reconciliation(seconds=min(15, max(0, until - time.monotonic())))
                    except Exception:pass
                report['settlement'] = cs.settle_unresolved(self.store, gateway.lookup, seconds=max(0, until - time.monotonic()-20))
            # Settlements live in the local DB: publish a durable accounting checkpoint (bounded wait).
            final = None
            try:
                final = self.checkpoint()
                ack_until = time.monotonic() + max(5, min(60, until - time.monotonic()))
                while not self.publisher.require_ack(final) and time.monotonic() < ack_until:time.sleep(.05)
                acked = bool(self.publisher.require_ack(final))
            except Exception as exc:
                acked = False; report['checkpoint_error'] = str(exc)[:200]
            report['checkpoint'] = dict(identity=final, acknowledged=acked)
            if not acked:
                # Never lose the settlements: write the accounting checkpoint straight to durable storage.
                # atomic_json fsyncs; it runs in a daemon thread bounded by the time left (<=60 s).
                import threading
                from .control import atomic_json as _aj
                box = {}
                def _write():
                    try:_aj(self.durable/'cancel-salvage-accounting.json', self.store.checkpoint());box['ok'] = True
                    except Exception as exc:box['error'] = str(exc)[:200]
                t = threading.Thread(target=_write, daemon=True);t.start()
                t.join(max(1, min(60, until - time.monotonic())))
                if box.get('ok'):
                    report['checkpoint']['direct_file'] = 'cancel-salvage-accounting.json'
                else:
                    report['checkpoint']['direct_file_error'] = box.get('error', 'direct write timed out')
                    report['durability_failed'] = True
        except Exception as exc:
            report['error'] = str(exc)[:200]
            if not (report.get('checkpoint') or {}).get('acknowledged') and not (report.get('checkpoint') or {}).get('direct_file'):
                report['durability_failed'] = True
        try:
            import json as _json, threading as _th
            target = self.durable/'cancel-salvage.json'
            text = _json.dumps(report, indent=1, default=str)
            t = _th.Thread(target=lambda: target.write_text(text), daemon=True); t.start(); t.join(10)  # report only; bounded
        except Exception:pass
        return report

    def prepare_replacement(self, *, release, release_sha256):
        from .cohort_restore import prepare
        return prepare(self, release=release, release_sha256=release_sha256)

    @classmethod
    def restore(cls, root, *, handoff_sha256, fake_providers=None, clock=time.time, live_transport=None):
        from .cohort_restore import restore
        return restore(cls, root, handoff_sha256=handoff_sha256,
                       fake_providers=fake_providers, clock=clock,live_transport=live_transport)
