"""Trusted closure catalog: durable fallback, exact-version status, final selection.

This API is controller-only. Do not expose it to worker tool dispatch. Compiler
results and authenticated verification decisions are supplied by trusted P2/P7
services; a worker's VERIFICATION.json never directly invokes record_status.
"""
import json
from pathlib import Path
import re
import time

from .closure_build import BuildResult, FALLBACK
from .closure_contracts import contract_directory, unix_ms
from .contracts_v3 import contracts_sha256, load_contracts
from .closure_io import ClosureError, atomic_json, canonical, locked, publish_tree, read_file, sha256, verify_tree

STATUSES = {'completion-unconfirmed', 'internal-supported', 'internal-rejected', 'indeterminate', 'withdrawn'}


def structural_completeness(files):
    """Mechanical flags only, deliberately no assertion of substantive adequacy."""
    source = files.get('FINAL.tex', b'').decode(errors='replace')
    # An even run of backslashes leaves % unescaped; an odd run makes it literal.
    # Strip before locating the body too, so commented document delimiters are inert.
    source = re.sub(r'(?<!\\)((?:\\\\)*)%[^\r\n]*', r'\1', source)
    body = source.split(r'\begin{document}', 1)[-1].split(r'\end{document}', 1)[0]
    words = re.findall(r'\b[A-Za-z]{2,}\b', re.sub(r'\\[A-Za-z]+', ' ', body))
    placeholders = bool(re.search(r'\b(TODO|TBD|PLACEHOLDER)\b', body, re.I))
    skeleton = len(words) < 120 or placeholders
    return {'label': 'skeleton' if skeleton else 'structurally-populated',
            'body_word_count': len(words), 'placeholder_marker': placeholders,
            'substantive_adequacy': 'ungraded', 'correctness': 'ungraded'}


class ClosureCatalog:
    def __init__(self, root, *, arm_id, problem_id, strategy=None, profile="production", artifact_root=None, assembler_id=None):
        self.root = Path(root).absolute()
        self.artifacts = Path(artifact_root or root).absolute()
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.arm_id, self.problem_id = arm_id, problem_id
        self.strategy = strategy or arm_id
        self.profile = profile
        policy = load_contracts(contract_directory())
        if profile not in {'production', 'pilot', 'swarm-check', 'solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue'} or self.strategy not in policy[profile]:
            raise ClosureError('known immutable arm strategy/profile required')
        if self.strategy.endswith('-swarm') and not assembler_id:
            raise ClosureError('swarm closure requires its declared assembler identity')
        self.assembler_id = assembler_id
        # Operator ruling ~15:25 PDT: a DeepSeek Flash backup writer may publish for swarms.
        author_keys = tuple(dict.fromkeys((policy[profile][self.strategy]['final_author'],'dsf'))) if self.strategy.endswith('-swarm') else policy[profile][self.strategy]['models']
        self.primary_model_id = policy['models'][policy[profile][self.strategy]['final_author']]['model_id']
        if self.strategy == 'mixed-swarm':
            # Writer pair from writer_style (measured 2026-10-11): Qwen 3.8 2.4T primary, Flash backup.
            from .writer_style import writer_pair
            author_keys = writer_pair('mixed-swarm')
            self.primary_model_id = policy['models'][author_keys[0]]['model_id']
        self.author_model_ids = sorted(policy['models'][key]['model_id'] for key in author_keys)
        if self.strategy == 'open-swarm':
            # Every full member writes; the vote orders the builds (winner last).
            row = policy[profile][self.strategy]
            self.author_model_ids = sorted(policy['models'][k]['model_id'] for k in row['open_members'])
            self.primary_model_id = None
        self.model_ids = sorted(policy['models'][key]['model_id'] for key in policy[profile][self.strategy]['models'])
        self.contracts_hash = contracts_sha256(contract_directory())
        with locked(self.root):
            if (self.artifacts / 'catalog-state.json').exists():
                atomic_json(self.root / 'catalog.json', json.loads(read_file(self.artifacts, 'catalog-state.json')))
            if not (self.root / 'catalog.json').exists():
                atomic_json(self.root / 'catalog.json', {
                    'schema': 1, 'arm_id': arm_id, 'problem_id': problem_id,
                    'strategy': self.strategy, 'profile': profile, 'model_ids': self.model_ids,
                    'assembler_id': self.assembler_id,
                    'contracts_sha256': self.contracts_hash, 'artifact_root': str(self.artifacts),
                    'fallback': None, 'bundles': [], 'rejected_candidates': {}, 'terminal': None})
            self._load()

    def _load(self):
        value = json.loads(read_file(self.root, 'catalog.json'))
        if value['arm_id'] != self.arm_id or value['problem_id'] != self.problem_id:
            raise ClosureError('catalog identity mismatch')
        if value.get('assembler_id') != self.assembler_id:
            raise ClosureError('declared assembler cannot change')
        if value['artifact_root'] != str(self.artifacts):
            raise ClosureError('catalog artifact root changed')
        if (value['strategy'], value['profile'], value['model_ids'], value['contracts_sha256']) != (
                self.strategy, self.profile, self.model_ids, self.contracts_hash):
            raise ClosureError('catalog immutable model/contract policy mismatch')
        return value

    def _save(self, value):
        atomic_json(self.artifacts / 'catalog-state.json', value)
        atomic_json(self.root / 'catalog.json', value)

    def _mutable(self, state):
        if state['terminal'] is not None:
            raise ClosureError('terminal catalog cannot be edited or resumed')

    def _publish(self, result, authorship, author_id, model_id):
        if not result.passed or result.metadata.get('outcome') != 'pass':
            raise ClosureError('only passing compiler results can be published')
        if not result.files.get('FINAL.pdf', b'').startswith(b'%PDF-') or not result.files.get('FINAL.tex'):
            raise ClosureError('passing bundle requires both source and compiled PDF')
        manifest = {'schema': 1, 'arm_id': self.arm_id, 'problem_id': self.problem_id,
                    'authorship': authorship, 'author_id': author_id, 'model_id': model_id,
                    'candidate_sha256': result.candidate_sha256,
                    'files': {k: sha256(v) for k, v in sorted(result.files.items())},
                    'compilation': result.metadata, 'structure': structural_completeness(result.files)}
        bundle_id = sha256(canonical(manifest))
        publish_tree(self.artifacts / 'bundles', bundle_id, {**result.files, 'bundle.json': canonical(manifest)})
        return {'bundle_id': bundle_id, **manifest, 'internal_status': 'completion-unconfirmed', 'status_evidence': []}

    def preflight(self, compiler):
        """Must return before P3 loads/adopts credentials or admits any inference."""
        with locked(self.root):
            state = self._load()
            self._mutable(state)
            if state['fallback']:
                self.verify_bundle(state['fallback'])
                return state['fallback']
            result = compiler.fallback()
            if result.files.get('FINAL.tex') != FALLBACK:
                raise ClosureError('administrative fallback source mismatch')
            record = self._publish(result, 'administrative-fallback', 'deterministic-service', None)
            state['fallback'] = record
            self._save(state)
            return record

    def publish_model(self, result, *, author_id, model_id, allowed_models=None, captured_at_ns=None, copy=False):
        if not result.passed or result.metadata.get('outcome')!='pass':
            raise ClosureError('only passing compiler results can be published')
        if allowed_models is not None and set(allowed_models)!=set(self.model_ids):
            raise ClosureError('caller allowlist differs from pinned arm policy')
        if captured_at_ns is None:captured_at_ns=result.metadata.get('writeup_time_ns')
        if not result.metadata.get('report_gate_fallback'):
            self.retain_draft(result.files, author_id=author_id, model_id=model_id,
                              captured_at_ns=captured_at_ns, compilation=result.metadata, copy=copy)
        with locked(self.root):
            state = self._load()
            self._mutable(state)
            if not state['fallback']:
                raise ClosureError('durable fallback is required before model work')
            if allowed_models is not None and set(allowed_models) != set(self.model_ids):
                raise ClosureError('caller allowlist differs from pinned arm policy')
            if self.assembler_id is not None and author_id != self.assembler_id:
                raise ClosureError('only the declared assembler may publish the primary report')
            if not author_id or model_id not in self.author_model_ids:
                raise ClosureError('author model is not allowed in this arm')
            record = self._publish(result, 'model', author_id, model_id)
            prior = next((b for b in state['bundles'] if b['bundle_id'] == record['bundle_id']), None)
            if prior:
                return prior
            # Retyping/rebuilding the identical rejected source does not revise it.
            if record['candidate_sha256'] in state.get('rejected_candidates', {}) or any(b['candidate_sha256'] == record['candidate_sha256'] and
                   b['internal_status'] in {'internal-rejected', 'withdrawn'} for b in state['bundles']):
                record['internal_status'] = 'internal-rejected'
            state['bundles'].append(record)
            self._save(state)
            return record

    def retain_failed_source(self, result, *, author_id, model_id, captured_at_ns=None, copy=False):
        """Keep model bytes when the compiler fails; this confers no support verdict."""
        if result.passed or result.metadata.get('outcome') == 'pass':
            raise ClosureError('degraded source requires a failed build')
        if not (result.files.get('FINAL.tex') or result.files.get('FINAL.md')) or result.files.get('FINAL.tex') == FALLBACK:
            raise ClosureError('degraded source requires model-authored bytes')
        if captured_at_ns is None:captured_at_ns=result.metadata.get('writeup_time_ns')
        self.retain_draft(result.files, author_id=author_id, model_id=model_id,
                          compilation=result.metadata, diagnostics=result.diagnostics, captured_at_ns=captured_at_ns, copy=copy)
        with locked(self.root):
            state = self._load(); self._mutable(state)
            if (not state['fallback'] or not author_id or model_id not in self.author_model_ids or
                    (self.assembler_id is not None and author_id != self.assembler_id)):
                raise ClosureError('degraded source author is not authorized')
            files = {k: v for k, v in result.files.items()
                     if k not in ('FINAL.pdf', 'bundle.json', 'DELIVERY.md')}
            files['DELIVERY.md'] = (
                '# Degraded model writeup\n\n'
                'The model source in FINAL.tex is preserved verbatim. Its PDF build failed. '
                'This is an unverified source delivery; it does not assert that the claims '
                'are correct or internally supported. Compiler diagnostics and provenance '
                'are recorded in bundle.json. No mathematical text was repaired.\n').encode()
            manifest = dict(schema=1, arm_id=self.arm_id, problem_id=self.problem_id,
                            authorship='model', author_id=author_id, model_id=model_id,
                            compilation=result.metadata, diagnostics=result.diagnostics,
                            candidate_sha256=result.candidate_sha256,
                            files={k:sha256(v) for k,v in sorted(files.items())},
                            internal_status='unverified', delivery='degraded-model-source')
            ident = sha256(canonical(manifest))
            publish_tree(self.artifacts/'degraded', ident, {**files, 'bundle.json':canonical(manifest)})
            row = dict(bundle_id=ident, **manifest)
            retained = state.setdefault('failed_builds', [])
            if not any(x['bundle_id'] == ident for x in retained): retained.append(row)
            self._save(state)
            return row

    def retain_draft(self, files, *, author_id, model_id, captured_at_ns=None,
                     compilation=None, diagnostics='', checks=None, copy=False):
        """Immutable model snapshot; not a verification decision or billing fence.

        copy=True marks a work/promotion copy: its mtime is not an author save, so
        bytes already retained keep their recorded save time."""
        source = files.get('FINAL.tex') or files.get('FINAL.md')
        if not source or source == FALLBACK:
            return None
        with locked(self.root):
            state = self._load(); self._mutable(state)
            if not state['fallback']:raise ClosureError('durable fallback is required before model work')
            if (not author_id or model_id not in self.author_model_ids or
                    (self.assembler_id is not None and author_id != self.assembler_id)):
                raise ClosureError('draft author is not authorized')
            files = {k:v for k,v in files.items() if k not in ('FINAL.pdf','bundle.json','DELIVERY.md')}
            candidate = BuildResult(False, files, '', {}).candidate_sha256
            saved = [d['captured_at_ns'] for d in state.get('drafts',[]) if d['candidate_sha256']==candidate]
            if copy and saved:captured_at_ns=max(saved)
            if captured_at_ns is None and state.get('drafts'):
                prior=state['drafts'][-1]
                if prior['files']=={k:sha256(v) for k,v in sorted(files.items())} and prior['compilation']==(compilation or {'outcome':'not-reached'}):return prior
            if checks is None:
                # Checks bind to the whole candidate, never to FINAL.tex alone.
                matches=[d for d in state.get('drafts',[]) if d['candidate_sha256']==candidate and d.get('checks') is not None]
                if matches:checks=matches[-1]['checks']
            manifest = dict(schema=1, arm_id=self.arm_id, problem_id=self.problem_id,
                            authorship='model', author_id=author_id, model_id=model_id,
                            captured_at_ns=captured_at_ns or time.time_ns(),
                            candidate_sha256=candidate,
                            compilation=compilation or {'outcome':'not-reached'},
                            diagnostics=diagnostics, checks=checks, structure=structural_completeness(files),
                            files={k:sha256(v) for k,v in sorted(files.items())},
                            internal_status='completion-unconfirmed', storage='drafts')
            ident = sha256(canonical(manifest))
            publish_tree(self.artifacts/'drafts', ident, {**files, 'bundle.json':canonical(manifest)})
            row = dict(bundle_id=ident, **manifest)
            retained = state.setdefault('drafts', [])
            if not any(x['bundle_id']==ident for x in retained):retained.append(row)
            self._save(state)
            return row

    def _deadline_selection(self, state):
        """Newest captured writeup, with a PDF only for its exact dependency bytes."""
        from .deadline_delivery import record_time, latest_key
        choices=[]
        for storage,key in (('drafts','drafts'),('bundles','bundles'),('degraded','failed_builds')):
            for row in state.get(key,[]):
                if row.get('authorship')!='model' or row['compilation'].get('report_gate_fallback'):continue
                candidate=dict(captured_at_ns=record_time(row,state,row.get('published_at_ns',0)),
                    files=row['files'],origin=str(self.artifacts/storage/row['bundle_id']))
                record=row if storage!='degraded' else dict(row,storage=storage,structure=structural_completeness({}))
                choices.append((candidate,record))
        return max(choices,key=lambda x:latest_key(x[0]))[1] if choices else state['fallback']

    def verify_selected(self, record):
        storage = record.get('storage','bundles')
        if storage == 'bundles':return self.verify_bundle(record)
        metadata = {k:v for k,v in record.items() if k!='bundle_id'}
        if storage=='degraded':metadata.pop('storage',None);metadata.pop('structure',None)
        hashes = {**record['files'], 'bundle.json':sha256(canonical(metadata))}
        verify_tree(self.artifacts/storage/record['bundle_id'],hashes)
        return hashes

    def record_status(self, bundle_id, *, candidate_sha256, status, authenticated_record_id):
        """P2/P4 must authenticate role, exact model/hash, limits and quorum first."""
        if status not in STATUSES or not authenticated_record_id:
            raise ClosureError('invalid authenticated internal decision')
        with locked(self.root):
            state = self._load()
            self._mutable(state)
            record = next((b for b in state['bundles'] if b['bundle_id'] == bundle_id), None)
            if record is None or record['candidate_sha256'] != candidate_sha256:
                raise ClosureError('verification does not bind this exact candidate')
            if record['internal_status'] in {'internal-rejected', 'withdrawn'} and status not in {'internal-rejected', 'withdrawn'}:
                raise ClosureError('rejected source must be revised before acceptance')
            if status in {'internal-rejected', 'withdrawn'}:
                state.setdefault('rejected_candidates', {})[candidate_sha256] = authenticated_record_id
            affected = [b for b in state['bundles'] if b['candidate_sha256'] == candidate_sha256] if status in {'internal-rejected', 'withdrawn'} else [record]
            for affected_record in affected:
                affected_record['internal_status'] = status
                if authenticated_record_id not in affected_record['status_evidence']:
                    affected_record['status_evidence'].append(authenticated_record_id)
            self._save(state)
            return record

    def record_candidate_rejection(self, candidate_sha256, *, authenticated_record_id):
        """Retain rejected source identity even when no build has passed yet.

        P2 obtains this identity with closure_build.source_identity on the exact
        authenticated verifier snapshot, never a mutable author lookalike file.
        """
        from .closure_contracts import digest
        digest(candidate_sha256)
        if not authenticated_record_id:
            raise ClosureError('authenticated rejection evidence required')
        with locked(self.root):
            state = self._load()
            self._mutable(state)
            state.setdefault('rejected_candidates', {})[candidate_sha256] = authenticated_record_id
            for record in state['bundles']:
                if record['candidate_sha256'] == candidate_sha256:
                    record['internal_status'] = 'internal-rejected'
                    if authenticated_record_id not in record['status_evidence']:
                        record['status_evidence'].append(authenticated_record_id)
            self._save(state)

    def record_snapshot_status(self, *, snapshot_sha256, snapshot_entries, status, authenticated_record_id):
        """Translate P2's full-evidence hash to matching compiled source candidates.

        P2 authenticates the session/model/cycle before this call. The full
        snapshot hash must bind all entries, and every compiled source dependency
        must be present with identical bytes. No match confers no support.
        """
        if sha256(canonical(snapshot_entries)) != snapshot_sha256:
            raise ClosureError('verification snapshot hash mismatch')
        with locked(self.root):
            state = self._load()
            matches = [record for record in state['bundles']
                       if all(snapshot_entries.get(name) == value for name, value in record['files'].items()
                              if name != 'FINAL.pdf')]
        # Separate transactions permit existing authenticated status machinery;
        # no author may publish concurrently (single trusted publisher).
        if not matches:
            raise ClosureError('verification snapshot matches no compiled candidate')
        for record in matches:
            self.record_status(record['bundle_id'], candidate_sha256=record['candidate_sha256'],
                               status=status, authenticated_record_id=authenticated_record_id)
        return [record['bundle_id'] for record in matches]

    def verify_bundle(self, record):
        hashes = dict(record['files'])
        fields = ('schema', 'arm_id', 'problem_id', 'authorship', 'author_id', 'model_id',
                  'candidate_sha256', 'files', 'compilation', 'structure')
        manifest = {key: record[key] for key in fields}
        if sha256(canonical(manifest)) != record['bundle_id']:
            raise ClosureError('bundle manifest identity changed')
        hashes['bundle.json'] = sha256(canonical(manifest))
        verify_tree(self.artifacts / 'bundles' / record['bundle_id'], hashes)
        return hashes

    def select_final(self):
        """Select immutable bytes for a compulsory solo check before freeze."""
        with locked(self.root):
            state = self._load()
            eligible = [b for b in state['bundles'] if b['internal_status'] not in {'internal-rejected', 'withdrawn'}]
            # Primary writer's model first, then any model (backup), then the placeholder.
            primary = [b for b in eligible if b.get('model_id') == getattr(self, 'primary_model_id', None)]
            eligible = [b for b in eligible if b not in primary] + primary
            selected = eligible[-1] if eligible else state['fallback']
            if selected is None:raise ClosureError('no durable final candidate')
            self.verify_bundle(selected)
            return selected

    def close(self, *, stop_reason, stopped_at, operational_cause=None, reviewed_bundle=None,
              verification_status=None, required_claim_manifest_sha256=None, check_summary=None, source_drafts=None):
        """No compile or model request at terminal selection; fallback already exists."""
        unix_ms(stopped_at)
        if not stop_reason:
            raise ClosureError('terminal stop reason required')
        from .deadline_writeup import capture
        if source_drafts: capture(self, source_drafts)
        with locked(self.root):
            state = self._load()
            if state['terminal'] is not None:
                self.verify_selected(state['terminal']['selected'])
                return state['terminal']
            reviewed_status = verification_status
            selected = self._deadline_selection(state)
            if selected is None:
                raise ClosureError('no durable terminal artifact; preflight was not completed')
            self.verify_selected(selected)
            fallback = selected['authorship'] == 'administrative-fallback'
            selected_status=('internal-rejected' if selected['candidate_sha256'] in state.get('rejected_candidates',{}) else selected['internal_status'])
            if (required_claim_manifest_sha256 is not None and selected['files'].get('CLAIMS-MANIFEST.json')!=required_claim_manifest_sha256):selected_status='stale-claim-authority; unverified'
            mechanical=next((d.get('checks') for d in reversed(state.get('drafts',[])) if d['candidate_sha256']==selected.get('candidate_sha256') and d.get('checks') is not None),None)
            supported = (not fallback and selected_status=='internal-supported' and
                         (required_claim_manifest_sha256 is None or
                          selected['files'].get('CLAIMS-MANIFEST.json')==required_claim_manifest_sha256))
            if mechanical and (mechanical.get('last_ok') is False or mechanical.get('error') or mechanical.get('author_cap_failure')):
                supported=False
            writer_selection = ('placeholder: no model draft exists' if fallback else
                                'latest model writeup at terminal stop')
            terminal = {'writer_selection': writer_selection,'schema': 1, 'arm_id': self.arm_id, 'problem_id': self.problem_id,
                        'stop_reason': stop_reason, 'stopped_at': stopped_at, 'operational_cause': operational_cause,
                        'selected': selected, 'model_delivery': 'failed' if fallback else 'compiling-model-bundle' if supported else 'unverified-model-writeup',
                        'structural_completeness': selected['structure'],
                        'internal_status': selected_status if not fallback else 'not-applicable',
                        'substantive_adequacy': 'ungraded', 'correctness': 'ungraded' if supported or fallback else 'unverified',
                        'rejected_evidence': [b['bundle_id'] for b in state['bundles'] if b['internal_status'] in {'internal-rejected', 'withdrawn'} and b['bundle_id'] != selected['bundle_id']],
                        'unconfirmed_evidence': [b['bundle_id'] for b in state['bundles'] if b['internal_status'] not in {'internal-supported', 'internal-rejected', 'withdrawn'} and b['bundle_id'] != selected['bundle_id']]}
            if verification_status is not None:
                # Never transfer another candidate's verdict to selected bytes.
                terminal['final_verification_status'] = (verification_status if selected.get('bundle_id') == reviewed_bundle
                    else selected_status if not fallback else 'administrative-no-claims')
            if verification_status is not None:
                terminal['reviewed_verification_status'] = reviewed_status
            if check_summary is not None:
                terminal['reviewed_check_summary'] = check_summary
            if not fallback:
                terminal['check_status_notice'] = (
                    'Latest model writeup released. Any claim not certified is unverified. '
                    'Compilation: '+json.dumps(selected['compilation'],sort_keys=True)+'. '
                    'Exact-source verification: '+terminal.get('final_verification_status',selected_status)+'. '
                    'Failed or incomplete checks: '+json.dumps(dict(semantic=check_summary,mechanical=mechanical),sort_keys=True)+'. '
                    'See REPORT-CHECK.md and compiler diagnostics when present.')
            # Preserve the old sidecar field for old failed-build catalogs.
            if selected.get('storage')=='degraded':terminal['degraded_delivery']=selected
            from .deadline_writeup import candidates, publish
            latest=publish(self.artifacts/'deadline',candidates(self.artifacts,state),
                           run=self.problem_id.removeprefix('b3-'),checks=check_summary)
            if latest:terminal['deadline_delivery']=latest
            delivery_status = ('Final report check status: ' + terminal.get('final_verification_status', terminal['internal_status'])
                + '\n' + terminal.get('check_status_notice', '')
                + '\nReviewed checks: ' + json.dumps(check_summary, sort_keys=True) + '\n').encode()
            publish_tree(self.artifacts / 'terminal', 'closure', {'closure.json': canonical(terminal), 'DELIVERY-STATUS.txt': delivery_status,
                 'DELIVERY.md': ('# Deadline writeup release\n\n'+terminal.get('check_status_notice','No model draft exists; administrative placeholder.')+'\n').encode()})
            state['terminal'] = terminal
            self._save(state)
            return terminal

    def scientific_files(self):
        """Enumerate all retained bundles and terminal provenance for scientific freeze."""
        with locked(self.root):
            state = self._load()
            if state['terminal'] is None:
                raise ClosureError('closure must finish before scientific freeze')
            result = {'closure.json': canonical(state['terminal']), 'DELIVERY-STATUS.txt': read_file(self.artifacts / 'terminal' / 'closure', 'DELIVERY-STATUS.txt'),
                      'DELIVERY.md':read_file(self.artifacts/'terminal/closure','DELIVERY.md')}
            if state['terminal'].get('deadline_delivery'):
                from .deadline_writeup import verify
                delivery=state['terminal']['deadline_delivery'];directory=verify(self.artifacts/'deadline',delivery)
                for name in delivery['files']:
                    result[name]=read_file(directory,name)
                    result['deadline/'+delivery['bundle_id']+'/'+name]=result[name]
                result['deadline/'+delivery['bundle_id']+'/delivery.json']=canonical({k:v for k,v in delivery.items() if k!='bundle_id'})
            for record in state.get('drafts', []):
                hashes=self.verify_selected(record)
                for name in hashes:
                    result['drafts/'+record['bundle_id']+'/'+name]=read_file(self.artifacts/'drafts'/record['bundle_id'],name)
            for record in state.get('failed_builds', []):
                metadata = {k:v for k,v in record.items() if k != 'bundle_id'}
                hashes = {**record['files'], 'bundle.json':sha256(canonical(metadata))}
                verify_tree(self.artifacts/'degraded'/record['bundle_id'], hashes)
                for name in hashes:
                    result['degraded/'+record['bundle_id']+'/'+name] = read_file(self.artifacts/'degraded'/record['bundle_id'], name)
            for record in [state['fallback'], *state['bundles']]:
                hashes = self.verify_bundle(record)
                for name in hashes:
                    result['bundles/' + record['bundle_id'] + '/' + name] = read_file(self.artifacts / 'bundles' / record['bundle_id'], name)
            return result
