"""Version 1 shared data contracts; no provider calls or runtime state mutation.

The inherited v2/contracts.py contains prompt text and is intentionally separate.
P9/P7 must package contracts/ alongside this module's parent directory, or pass an
explicit pinned directory to load_contracts. Loading policy does not admit a run.
"""
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

CONTRACT_VERSION = 1
CANDIDATE_COMMIT = '7e0a5ec19b9d705a513119ef14fd96955e2659e8'
SMOKE_SHA256 = 'abfc750c0823f5f71e256a59e2260d2d3862fad9c3fbba875ef92d26f7764f75'
CONTRACT_FILES = ('profiles.json', 'records.schema.json', 'roles.json', 'orchestration.schema.json')


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate contract key: ' + key)
        result[key] = value
    return result


def canonical_json(value):
    """Canonical bytes for JSON wire records (integer money and timestamps)."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def record_sha256(value):
    return sha256(canonical_json(value)).hexdigest()


def _contract_bytes(directory):
    directory = Path(directory) if directory else Path(__file__).resolve().parents[1] / 'contracts'
    files = {}
    for name in CONTRACT_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('contract must be a regular, non-symlink file: ' + name)
        files[name] = path.read_bytes()
    return files


def _contracts_digest(files):
    h = sha256()
    for name in CONTRACT_FILES:
        data = files[name]
        h.update(name.encode('ascii') + b'\0' + str(len(data)).encode('ascii') + b'\0' + data)
    return h.hexdigest()


def contracts_sha256(directory=None):
    return _contracts_digest(_contract_bytes(directory))


def orchestration_schema(*, expected_sha256):
    files = _contract_bytes(None)
    if _contracts_digest(files) != expected_sha256:
        raise ValueError('contract hash mismatch')
    return json.loads(files['orchestration.schema.json'], object_pairs_hook=_unique_object)


def load_contracts(directory=None, *, expected_sha256=None, apply_continuation=True):
    """Return deeply immutable profiles. Expected hash is required at launch."""
    files = _contract_bytes(directory)
    digest = _contracts_digest(files)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('contract hash mismatch')
    # Parse the same bytes that were hashed, never reopen after verification.
    data = json.loads(files['profiles.json'], object_pairs_hook=_unique_object)
    if data.get('contract_version') != CONTRACT_VERSION:
        raise ValueError('unsupported contract version')
    validate_profiles(data)
    derive_swarm_check(data)
    derive_solo_check(data)
    derive_closing_check(data)
    derive_prod_check(data)
    derive_open_swarm(data)
    derive_prod_continue(data)
    if apply_continuation:
        from .production_continuation import apply
        apply(data)
    return _freeze(data)


LIVE_PROFILES = ('pilot', 'production', 'swarm-check', 'solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue','open-continue')

SOLO_CHECK = dict(cap_microusd=60_000_000, report_microusd=2_000_000, research_stop_seconds=1800, wall_seconds=3600)  # ~22 min write-up (coordinator ~16:35)
SOLO_ARMS = ('dsf-solo', 'dsp-solo', 'qwen24t-solo', 'k3-solo')


def derive_solo_check(data):
    """Four solo arms, one hour, $10 each, from the pilot solo rows (in memory only)."""
    rows = {}
    for arm in SOLO_ARMS:
        row = json.loads(json.dumps(data['pilot'][arm]))
        row.update(cap_microusd=SOLO_CHECK['cap_microusd'], wall_seconds=SOLO_CHECK['wall_seconds'],
                   research_stop_seconds=SOLO_CHECK['research_stop_seconds'], report_reserve_microusd=SOLO_CHECK['report_microusd'],
                   pools_microusd={'report': SOLO_CHECK['report_microusd'], 'research_and_verification': SOLO_CHECK['cap_microusd'] - SOLO_CHECK['report_microusd']})
        rows[arm] = row
    data['solo-check'] = rows
    data['solo-check_total_microusd'] = sum(r['cap_microusd'] for r in rows.values())
# Operator ruling 2026-10-05: replies uncapped (provider maximum), so every
# closing reserve holds at least one request at that maximum: DSF $1.15, K3 $14.35,
# Qwen 2.4T $0.91. mixed-swarm gets $60 to carry its K3 orchestrator and writer.
# diag-1231 (contributing): a 225 s draft could not fit one uncapped writer reply;
# closing is 25 minutes from minute 35.
# operator 18:45: checks are never money-bound ($60 per arm)
SWARM_CHECK = dict(cap_microusd={'dsf-swarm': 60_000_000, 'mixed-swarm': 60_000_000}, research_stop_seconds=2100, wall_seconds=3600,
    closing_stages_seconds=[60, 600, 240, 300, 180, 120],
    closing_reserves_microusd={'dsf-swarm': {'writer': 2000000, 'verifier-a': 1500000, 'verifier-b': 1500000, 'control': 1500000},
                               'mixed-swarm': {'writer': 15000000, 'verifier-a': 1500000, 'verifier-b': 1500000, 'control': 15000000}})


def derive_swarm_check(data):
    """Two-arm, one-hour real-problem swarm check derived from the accepted pilot
    swarm rows. It lives only in memory, so the accepted contract bytes and hash
    are unchanged; production and pilot rows are never modified."""
    rows = {}
    for arm in ('dsf-swarm', 'mixed-swarm'):
        row = json.loads(json.dumps(data['pilot'][arm]))
        reserves = SWARM_CHECK['closing_reserves_microusd'][arm]
        row.update(cap_microusd=SWARM_CHECK['cap_microusd'][arm], research_stop_seconds=SWARM_CHECK['research_stop_seconds'],
                   wall_seconds=SWARM_CHECK['wall_seconds'], closing_stages_seconds=list(SWARM_CHECK['closing_stages_seconds']),
                   closing_max_seconds=sum(SWARM_CHECK['closing_stages_seconds']), revision_rounds=1,
                   closing_reserves_microusd=dict(reserves), report_reserve_microusd=sum(reserves.values()))
        if row['research_stop_seconds'] + row['closing_max_seconds'] != row['wall_seconds']:
            raise ValueError('swarm-check closing schedule mismatch')
        rows[arm] = row
    data['swarm-check'] = rows
    data['swarm-check_total_microusd'] = sum(r['cap_microusd'] for r in rows.values())


def _money(value):
    if type(value) is not int or value < 0:
        raise ValueError('money must be nonnegative integer microdollars')
    return value


def validate_profiles(data):
    """Check partition arithmetic and rosters; live route readiness is separate."""
    expected_models = {'dsf', 'dsp', 'qwen24t', 'k3', 'glm'}
    if set(data['models']) != expected_models:
        raise ValueError('unexpected model roster')
    expected_arms = {'dsf-solo', 'dsp-solo', 'qwen24t-solo', 'k3-solo', 'dsf-swarm', 'mixed-swarm'}
    for profile in ('production', 'pilot'):
        arms = data[profile]
        if set(arms) != expected_arms:
            raise ValueError('unexpected arm roster')
        total = 0
        for name, arm in arms.items():
            cap = _money(arm['cap_microusd'])
            total += cap
            if profile == 'production' and cap != 200_000_000:
                raise ValueError('production arm cap must be $200')
            roster = set(arm['models'])
            expected = ({name[:-5]} if name.endswith('-solo') else
                        {'dsf'} if name == 'dsf-swarm' else expected_models)
            if roster != expected or ('slots' in arm and set(arm['slots']) != roster):
                raise ValueError('arm model roster mismatch')
            if any(type(x) is not int or x < 1 for x in arm.get('slots', {}).values()):
                raise ValueError('invalid worker slots')
            if arm.get('orchestration') == 'stateless-v1':
                expected_author = 'k3' if name == 'mixed-swarm' else 'dsf'
                if arm['final_author'] != expected_author or arm['orchestrator'] != expected_author:
                    raise ValueError('swarm model ownership mismatch')
                forbidden = {'slots', 'pools_microusd', 'model_pools_microusd', 'review_ticket_limit', 'glm_ticket_limit'}
                if forbidden.intersection(arm):
                    raise ValueError('retired research allocations')
                reserve = arm['closing_reserves_microusd']
                target = ([15000000,6000000,6000000,15000000] if name == 'mixed-swarm' else [12000000,6000000,6000000,6000000]) if profile == 'production' else ([6000000,3000000,3000000,4000000] if name == 'mixed-swarm' else [4000000,3000000,3000000,2000000])
                if [reserve[k] for k in ('writer','verifier-a','verifier-b','control')] != target:
                    raise ValueError('closing role guarantees mismatch')
                if sum(arm['closing_stages_seconds']) != arm['closing_max_seconds'] or arm['research_stop_seconds'] + arm['closing_max_seconds'] != arm['wall_seconds']:
                    raise ValueError('closing schedule mismatch')
                from .output_limits import LARGEST
                if arm['request_limits']!={'input_tokens':64000,'completion_tokens':LARGEST}:
                    raise ValueError('orchestrator request bounds mismatch')
                minimum=arm['minimum_request']
                if set(minimum)!=set(arm['request_limits']) or any(type(v) is not int or not 0<v<=arm['request_limits'][k] for k,v in minimum.items()):
                    raise ValueError('configured minimum request invalid')
                if arm.get('orchestrator_failure_limit')!=3 or type(arm.get('queued_assignment_limit')) is not int or arm['queued_assignment_limit']<1:
                    raise ValueError('orchestration infrastructure bounds missing')
                expected_stages=[240,3600,1500,1200,900,1200,900,600] if profile=='production' else [180,900,900,600,720,300]
                if arm['closing_stages_seconds']!=expected_stages or arm['revision_rounds']!=(2 if profile=='production' else 1):
                    raise ValueError('mandatory stage schedule changed')
                if arm['research_stop_seconds']!=(76260 if profile=='production' else 7200) or arm['wall_seconds']!=(86400 if profile=='production' else 10800):
                    raise ValueError('original closing or wall bound changed')
                amount = cap
                report = sum(_money(v) for v in reserve.values())
            elif 'model_pools_microusd' in arm:
                pools = arm['model_pools_microusd']
                if set(pools) != roster:
                    raise ValueError('model pools mismatch')
                amount = sum(_money(v) for p in pools.values() for v in p.values()) + _money(arm['recovery_microusd'])
                report = sum(p['report'] for p in pools.values())
            else:
                amount = sum(_money(v) for v in arm['pools_microusd'].values())
                report = arm['pools_microusd']['report']
            if amount != cap or report != _money(arm['report_reserve_microusd']):
                raise ValueError('pool partition does not conserve arm cap')
        if profile == 'pilot':
            total += _money(data['pilot_shared_recovery_microusd'])
        if total != data[profile + '_total_microusd']:
            raise ValueError('cohort cap mismatch')


def validate_route(model_key, *, model_id, provider, effort, contracts=None):
    """Exact positive allowlist, applicable to every primary/secondary call."""
    contracts = contracts if contracts is not None else load_contracts()
    model = contracts['models'].get(model_key)
    if model is None or (model_id, provider, effort) != (model['model_id'], model['provider'], model['effort']):
        raise ValueError('undeclared model, provider or effort')
    return True


def validate_arm_manifest(arm, *, contracts=None):
    """Semantic checks in addition to records.schema.json's structural checks."""
    contracts = contracts if contracts is not None else load_contracts()
    if arm['contract_version'] != CONTRACT_VERSION:
        raise ValueError('unsupported contract version')
    policy = contracts[arm['profile']][arm['strategy']]
    if set(arm['model_keys']) != set(policy['models']) or len(arm['model_keys']) != len(policy['models']):
        raise ValueError('arm model roster mismatch')
    if arm['original_cap_microusd'] != policy['cap_microusd'] or arm['report_reserve_microusd'] != policy['report_reserve_microusd']:
        raise ValueError('immutable allocation mismatch')
    if type(arm['started_at_unix_ms']) is not int or type(arm['deadline_unix_ms']) is not int:
        raise ValueError('integer absolute timestamps required')
    if arm['started_at_unix_ms'] < 0 or not 0 < arm['deadline_unix_ms'] - arm['started_at_unix_ms'] <= policy['wall_seconds'] * 1000:
        raise ValueError('invalid original deadline')
    if arm['test_only'] is not (arm['profile'] == 'pilot'):
        raise ValueError('test flags forbidden in production')
    return True


def validate_attempt_partition(attempt):
    """Estimates annotate liability, never add a fourth charged partition."""
    fields = ('settled_microusd', 'unresolved_upper_microusd', 'active_reservation_microusd')
    values = [_money(attempt[k]) for k in fields]
    index = {'settled': 0, 'unresolved': 1, 'reserved': 2}[attempt['state']]
    if any(value for i, value in enumerate(values) if i != index):
        raise ValueError('attempt charged in more than one partition')
    bound = _money(attempt['upper_bound_microusd'])
    released = _money(attempt['released_microusd'])
    if sum(values) + released != bound:
        raise ValueError('attempt liability not conserved')
    if attempt['state'] != 'settled' and released:
        raise ValueError('unsettled liability cannot be released')
    if attempt['estimate_microusd'] is not None:
        if _money(attempt['estimate_microusd']) > bound:
            raise ValueError('estimate exceeds admitted bound')
    return True


def validate_ledger_partition(snapshot):
    cap = _money(snapshot['original_cap_microusd']) + _money(snapshot['recovery_grant_microusd'])
    accounted = sum(_money(snapshot[k]) for k in ('settled_microusd', 'unresolved_upper_microusd', 'active_reservation_microusd', 'available_microusd'))
    if cap != accounted or snapshot['conservation_residual_microusd'] != 0:
        raise ValueError('unexplained ledger residual')
    return True


def validate_cohort_export(export):
    """Check complete roster coverage; P6 verifies physical terminal evidence."""
    registered = export['registered_arm_ids']
    terminal = export['terminal_records']
    ids = [r['arm_id'] for r in terminal]
    if not registered or len(set(registered)) != len(registered) or len(set(ids)) != len(ids) or set(ids) != set(registered):
        raise ValueError('cohort embargo: missing or duplicate terminal record')
    if any(r['kind'] not in ('frozen', 'withdrawn') for r in terminal) or export['resume_prohibited'] is not True:
        raise ValueError('cohort embargo: nonterminal arm or resume permitted')
    return True


def amended_contracts(accepted_sha, current_sha, root):
    """True when current contracts equal the accepted ones or a recorded operator amendment of them."""
    import json as _json
    if accepted_sha == current_sha:return True
    path=Path(root)/'deployment/contract-amendments.json'
    if not path.exists():return False
    chain={row['from']:row['to'] for row in _json.loads(path.read_text())}
    seen=accepted_sha
    while seen in chain:
        seen=chain[seen]
        if seen==current_sha:return True
    return False


# Closing-focused swarm check (coordinator, 2026-10-05): same arms and budgets as the
# swarm check, research stops at 8 minutes, closing ends by 25 minutes.
# diag-1339: recheck needs >= 4 min; K3's writer needs more draft time. 30 min wall.
CLOSING_CHECK = dict(research_stop_seconds=480, wall_seconds=3600, closing_stages_seconds=[30, 2400, 240, 180, 180, 90])  # operator ~15:25 PDT: 60-min check, 40-min draft


def derive_closing_check(data):
    rows = {}
    for arm, base in data['swarm-check'].items():
        row = json.loads(json.dumps(base))
        row.update(research_stop_seconds=CLOSING_CHECK['research_stop_seconds'], wall_seconds=CLOSING_CHECK['wall_seconds'],
                   closing_stages_seconds=list(CLOSING_CHECK['closing_stages_seconds']),
                   closing_max_seconds=sum(CLOSING_CHECK['closing_stages_seconds']))
        if row['research_stop_seconds'] + row['closing_max_seconds'] != row['wall_seconds']:
            raise ValueError('closing-check schedule mismatch')
        rows[arm] = row
    data['closing-check'] = rows
    data['closing-check_total_microusd'] = sum(r['cap_microusd'] for r in rows.values())


# Production-profile check (coordinator 18:3x PDT after production p02 failed):
# the production swarm rows unchanged (caps, reserves, models, writers) except the
# clock: 1-hour wall, research 30 min, closing stages scaled to 30 min.
PROD_CHECK = dict(research_stop_seconds=1800, wall_seconds=3600, closing_stages_seconds=[60, 900, 240, 180, 150, 90, 90, 90])


def derive_prod_check(data):
    rows = {}
    for arm in ('dsf-swarm', 'mixed-swarm'):
        row = json.loads(json.dumps(data['production'][arm]))
        row.update(research_stop_seconds=PROD_CHECK['research_stop_seconds'], wall_seconds=PROD_CHECK['wall_seconds'],
                   closing_stages_seconds=list(PROD_CHECK['closing_stages_seconds']),
                   closing_max_seconds=sum(PROD_CHECK['closing_stages_seconds']))
        if row['research_stop_seconds'] + row['closing_max_seconds'] != row['wall_seconds'] or len(row['closing_stages_seconds']) != len(data['production'][arm]['closing_stages_seconds']):
            raise ValueError('prod-check schedule mismatch')
        rows[arm] = row
    data['prod-check'] = rows
    data['prod-check_total_microusd'] = sum(r['cap_microusd'] for r in rows.values())


# Open swarm (operator ~18:15 PDT): no orchestrator. Full members dsf, dsp, k3,
# qwen24t; glm is a light member (comments, critique, vote). Closing: each full
# member writes, all critique, full members rewrite, all vote. Derived from the
# production mixed-swarm row (same models, accounts and routes).
OPEN_MEMBERS = ('dsf', 'dsp', 'k3', 'qwen24t')
OPEN_LIGHT = ('glm',)
# Closing phases as fractions of the closing window before the freeze stage.
OPEN_PHASES = (('write', 0.32), ('critique', 0.24), ('rewrite', 0.24), ('vote', 0.20))  # check 3: critics/voters ran out of time
OPEN_PROFILES = {
    'open-check': dict(cap_microusd=60_000_000, wall_seconds=3600, research_stop_seconds=1500,
                       closing_stages_seconds=[60, 1740, 60, 60, 60, 120]),
    'open-production': dict(cap_microusd=200_000_000, wall_seconds=86400, research_stop_seconds=76260,
                            closing_stages_seconds=[240, 9000, 60, 60, 60, 120, 60, 540]),
    # Seeded continuation of open production 1790863 (02:05 PDT start; replaced by
    # about 07:55): 24 h run time minus the 5 h 50 min already run.
    'open-continue': dict(cap_microusd=200_000_000, wall_seconds=10260, research_stop_seconds=10260 - 10140,
                          closing_stages_seconds=[240, 9000, 60, 60, 60, 120, 60, 540]),
}


def derive_open_swarm(data):
    for profile, spec in OPEN_PROFILES.items():
        base = 'production' if profile in ('open-production', 'open-continue') else 'prod-check'
        row = json.loads(json.dumps(data[base]['mixed-swarm']))
        if len(spec['closing_stages_seconds']) != len(row['closing_stages_seconds']):
            row['revision_rounds'] = 1 if len(spec['closing_stages_seconds']) == 6 else 2
        row.update(cap_microusd=spec['cap_microusd'], wall_seconds=spec['wall_seconds'],
                   research_stop_seconds=spec['research_stop_seconds'],
                   closing_stages_seconds=list(spec['closing_stages_seconds']),
                   closing_max_seconds=sum(spec['closing_stages_seconds']),
                   models=list(OPEN_MEMBERS + OPEN_LIGHT), open_swarm=True,
                   open_members=list(OPEN_MEMBERS), open_light=list(OPEN_LIGHT), open_phases=[list(p) for p in OPEN_PHASES])
        if row['research_stop_seconds'] + row['closing_max_seconds'] != row['wall_seconds']:
            raise ValueError(profile + ' schedule mismatch')
        data[profile] = {'open-swarm': row}
        data[profile + '_total_microusd'] = row['cap_microusd']


# Continuation of the production swarms (operator 2026-10-06): production rows,
# closing stages unchanged, wall = the remaining time to 24 h from the original
# 19:45 PDT start, minus startup (13.5 h when launched by about 06:15 PDT).
PROD_CONTINUE_WALL = 48600
# mixed-swarm continues after its ~07:20 freeze: 12 h keeps it near the original 19:45 end.
# Write-up-only continuations: 120 s research window + a 90-minute closing (operator 2026-10-11 12:00;
# Qwen write-ups took a median 13 min in the writer comparison). Same eight stages, two revision rounds.
PROD_CONTINUE_CLOSING_STAGES = (120, 1800, 900, 720, 480, 600, 480, 300)
PROD_CONTINUE_WALLS = {'dsf-swarm': 120 + sum(PROD_CONTINUE_CLOSING_STAGES), 'mixed-swarm': 120 + sum(PROD_CONTINUE_CLOSING_STAGES)}


def derive_prod_continue(data):
    rows = {}
    for arm in ('dsf-swarm', 'mixed-swarm'):
        row = json.loads(json.dumps(data['production'][arm]))
        wall = PROD_CONTINUE_WALLS[arm]
        row.update(closing_stages_seconds=list(PROD_CONTINUE_CLOSING_STAGES), closing_max_seconds=sum(PROD_CONTINUE_CLOSING_STAGES))
        row.update(wall_seconds=wall, research_stop_seconds=wall - row['closing_max_seconds'])
        rows[arm] = row
    data['prod-continue'] = rows
    data['prod-continue_total_microusd'] = sum(r['cap_microusd'] for r in rows.values())
