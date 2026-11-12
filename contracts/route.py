"""Pinned OpenRouter route authority shared by research, closing and writing.

profiles.json supplies data; this module alone interprets it or constructs wire routes.
No health probe, fallback or restored state can widen a contract.
"""
import json
from pathlib import Path
import re


class OffRoute(RuntimeError):
    pass


def profiles(run=None):
    if run is None:
        return json.loads(Path(__file__).with_name('profiles.json').read_text())['models']
    if isinstance(run, (str, Path)):
        run = json.loads(Path(run).read_text())
    if 'models' in run and 'contracts_sha256' not in run:
        return run['models']
    # Launch contracts bind the exact profiles bytes, not a mutable settings file.
    from v2.contracts_v3 import load_contracts
    return load_contracts(expected_sha256=run['contracts_sha256'])['models']


def pinned_route(run=None, model=None):
    rows = profiles(run)
    if model is None:
        roster=(run.get('arm_profiles',{}).get('mixed-swarm',{}).get('models',list(rows))
                if isinstance(run,dict) else list(rows))
        return {k:(v['model_id'],v['provider']) for k,v in rows.items() if k in roster}
    key = model if model in rows else next((k for k,v in rows.items() if v['model_id']==model), None)
    if key is None:
        raise ValueError('model absent from pinned route contract: '+str(model))
    row=rows[key]
    if row.get('allow_fallbacks') is not False:
        raise ValueError('pinned contract must disable fallbacks')
    tags=row['allowed_hosts']
    if not isinstance(tags,(list,tuple)) or not tags or any(not isinstance(t,str) or not t for t in tags):
        raise ValueError('pinned contract requires allowed served hosts')
    provider=dict(only=list(tags), order=list(tags), allow_fallbacks=False,
                  require_parameters=row.get('require_parameters', True))
    quant=row['provider'].rsplit('/',1)[-1]
    if quant in {'fp8','mxfp4','bf16','fp16','fp32','int8','fp4','int4'}:
        provider['quantizations']=[quant]
    return dict(model=row['model_id'], provider=provider)


def build(request, model=None, run=None, *, host=None):
    result=dict(request)
    result.update(pinned_route(run, model or result.get('model')))
    if 'max_price' in request.get('provider',{}):result['provider']['max_price']=request['provider']['max_price']
    if host is not None:
        if host not in result['provider']['only']:raise ValueError('host outside pinned route contract: '+str(host))
        result['provider']['only']=[host];result['provider']['order']=[host]
    return result


def routes(run=None):
    return {k:(v['model_id'], v['provider']) for k,v in profiles(run).items()}


def _host(value):
    return re.sub('[^a-z0-9]', '', str(value).lower().split('/')[0])


def check_model(pinned, model):
    if model and model!=pinned and not re.fullmatch(re.escape(pinned)+r'-[0-9]{8}',model):
        raise OffRoute('served model outside pinned route: '+str(model))


def check(request, response):
    """Use served metadata, never infer the host from the requested route.

    Quantization is constrained on the wire; when returned it is checked too.
    OpenRouter commonly returns display names and dated canonical model IDs.
    """
    metadata=response.get('data') if isinstance(response.get('data'),dict) else response
    hosts=[metadata[k] for k in ('provider_name','provider') if metadata.get(k)]
    expected=request['provider']['only']
    if not hosts or any(not isinstance(host,str) or not any(_host(t)==_host(host) for t in expected) for host in hosts):
        raise OffRoute('served provider outside pinned route: '+str(hosts))
    host=hosts[0]
    model=metadata.get('model')
    pinned=request['model']
    check_model(pinned,model)
    quant=metadata.get('quantization')
    allowed=request['provider'].get('quantizations')
    if quant and allowed and quant.lower() not in allowed:
        raise OffRoute('served quantization outside pinned route: '+str(quant))
    return host


# Legacy resilience observations and offline eligibility utilities.
"""Provider resilience (design: status/provider-resilience.md, approved 2026-10-06).

Eligibility per model, from OpenRouter's endpoints list at preflight:
  full shape required: max output >= the run's reply shape (no reply cap), context >= required.
  preferred: stated precision (the pinned route's class if it states one, else the best stated class).
  if fewer than 2 preferred: add unstated-precision endpoints (operator ruling); stated low-bit
  endpoints are never added when a higher stated precision exists.
Health is per (model, provider): 3 consecutive failures mark a provider down for the run;
a down provider is re-probed every 15 min with a 1-token call and rejoins on success.
Agents of one model are spread round-robin over its eligible providers (first choice);
the remaining eligible providers follow as ordered fallbacks; allow_fallbacks stays False.
A measured ROUTE_CHOICE overrides that per model: a named provider leads every request, or
'router' lets OpenRouter choose among every full-shape endpoint (no order sent).
"""
import json
import os
import threading
import time
import urllib.request

# Static fallbacks used before a preflight has run (tests, legacy callers).
RANK = {'bf16': 3, 'fp16': 3, 'fp32': 3, 'fp8': 2, 'int8': 2, 'mxfp4': 1, 'fp4': 1, 'int4': 1}
DOWN_AFTER = 3
REPROBE_SECONDS = 900

_lock = threading.RLock()
_eligible = {}      # model -> [tag, ...] in preference order
_health = {}        # (model, tag) -> dict(fails, down_since, last_probe, calls, errors)
_assign = {}        # model -> next round-robin index
_log = []           # events: down / up / pause


def _stated(e):
    q = (e.get('quantization') or '').lower()
    return q if q and q != 'unknown' else None


def eligible_from_endpoints(endpoints, pinned, shape):
    full = [e for e in endpoints if e.get('context_length', 0) >= shape['context_tokens']
            and (e.get('max_completion_tokens') or 0) >= shape['completion_tokens']]
    pin = next((e for e in endpoints if e.get('tag') == pinned), None)
    pin_q = _stated(pin) if pin else (pinned.split('/')[-1].lower() if pinned.split('/')[-1].lower() in RANK else None)
    stated = [e for e in full if _stated(e) in RANK]
    if pin_q:
        preferred = [e for e in stated if _stated(e) == pin_q]
    else:
        best = max((RANK[_stated(e)] for e in stated), default=0)
        preferred = [e for e in stated if RANK[_stated(e)] == best]
    tags = [e['tag'] for e in preferred]
    # The pinned route stays eligible whenever it still serves the full shape.
    if any(e['tag'] == pinned for e in full) and pinned not in tags:
        tags.insert(0, pinned)
    if len(tags) < 2:
        tags += [e['tag'] for e in full if _stated(e) is None and e['tag'] not in tags]
    # The pinned route first when it is still eligible, then by uptime.
    up = {e['tag']: e.get('uptime_last_30m') or 0 for e in endpoints}
    tags = sorted(dict.fromkeys(tags), key=lambda t: (t != pinned, -up.get(t, 0)))
    return tags, [e for e in endpoints if e.get('tag') in tags]


def choose(model, tags, endpoints, shape):
    """Apply ROUTE_CHOICE to a preflight's eligible tags. A chosen provider is added first when
    it serves the full shape; 'router' widens to every full-shape endpoint except stated
    precisions below the best stated one."""
    try:choice=pinned_route(model=model)['provider']['order'][0]
    except ValueError:choice=None
    full = [e for e in endpoints if e.get('context_length', 0) >= shape['context_tokens']
            and (e.get('max_completion_tokens') or 0) >= shape['completion_tokens']
]
    if choice == 'router':
        best = max((RANK[_stated(e)] for e in full if _stated(e) in RANK), default=0)
        wide = [e['tag'] for e in full if _stated(e) not in RANK or RANK[_stated(e)] >= best]
        return list(dict.fromkeys(tags + wide))
    if choice and any(e['tag'] == choice for e in full):
        return [choice] + [t for t in tags if t != choice]
    return tags


def request_provider(model, route_order):
    """The OpenRouter provider object for one request."""
    if model in ('synthetic','fixture'):
        return dict(only=list(route_order),allow_fallbacks=False,require_parameters=True)
    provider=pinned_route(model=model)['provider']
    allowed=provider['only']
    provider['order']=list(dict.fromkeys([t for t in route_order if t in allowed]+allowed))
    return provider


def set_eligible(model, tags):
    with _lock:
        _eligible[model] = list(tags)


def eligible(model, pinned):
    with _lock:
        tags = list(_eligible.get(model) or [])
    if not tags:
        try:tags=pinned_route(model=model)['provider']['only']
        except ValueError:tags=[pinned]
    return tags


def providers(model, pinned):
    """Every provider accepted for this model (accounting, preflight)."""
    return pinned_route(model=model)['provider']['only']


def _h(model, tag):
    return _health.setdefault((model, tag), dict(fails=0, down_since=None, last_probe=0, calls=0, errors=0))


def is_down(model, tag):
    with _lock:
        return _h(model, tag)['down_since'] is not None


def assign(model):
    with _lock:
        k = _assign.get(model, 0); _assign[model] = k + 1
        return k


def order(model, pinned, slot_index):
    """Ordered provider list for one request: this agent's round-robin first choice, then the
    rest; providers marked down go last (they are tried only if every other one fails)."""
    tags = eligible(model, pinned)
    # A measured lead provider is never rotated away from.
    try:preferred=pinned_route(model=model)['provider']['order'][0]
    except ValueError:preferred=None
    k = 0 if preferred == 'modal' else slot_index % len(tags)
    rot = tags[k:] + tags[:k]
    with _lock:
        up = [t for t in rot if _h(model, t)['down_since'] is None]
        down = [t for t in rot if t not in up]
    return up + down, bool(up)


def available(model, pinned):
    return any(not is_down(model, t) for t in eligible(model, pinned))


def record(model, tag, ok, now=None):
    now = time.time() if now is None else now
    with _lock:
        h = _h(model, tag); h['calls'] += 1
        if ok:
            if h['down_since'] is not None:_log.append(dict(event='up', model=model, provider=tag, at=now))
            h['fails'] = 0; h['down_since'] = None
        else:
            h['errors'] += 1; h['fails'] += 1
            if h['fails'] >= DOWN_AFTER and h['down_since'] is None:
                h['down_since'] = now; h['last_probe'] = now
                _log.append(dict(event='down', model=model, provider=tag, at=now, consecutive=h['fails']))


def due_probes(now=None):
    now = time.time() if now is None else now
    with _lock:
        out = []
        for (model, tag), h in _health.items():
            if h['down_since'] is not None and now - h['last_probe'] >= REPROBE_SECONDS:
                h['last_probe'] = now; out.append((model, tag))
        return out


def probe(model, tag, key, timeout=60, charge=None):
    """One small call pinned to exactly this provider. charge(cost_microusd) books its cost
    in the cohort accounting (review 2a62c74 #5)."""
    body = dict(model=model, messages=[dict(role='user', content='Reply with OK')], max_tokens=16,
                usage=dict(include=True))
    from contracts.route import build, check
    body=build(body,model,host=tag)
    ok = False
    try:
        r = urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
            data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}),
            timeout=timeout)
        d = json.loads(r.read()); ok = r.status == 200 and 'choices' in d
        cost = (d.get('usage') or {}).get('cost')
        if charge is not None:charge(int(round(float(cost) * 1e6)) if cost is not None else 1000)
        check(body,d)
    except OffRoute as exc:
        with _lock:_log.append(dict(event='off-route-rejected',model=model,provider=d.get('provider'),error=str(exc),at=time.time()))
        ok=False
    except Exception:
        ok = False
    with _lock:
        if ok:
            _h(model, tag).update(fails=0, down_since=None)
            _log.append(dict(event='up', model=model, provider=tag, at=time.time(), via='probe'))
        else:
            _log.append(dict(event='probe-failed', model=model, provider=tag, at=time.time()))
    return ok


def restore(saved):
    """Review 2a62c74 #2: health survives a controller restore. Eligibility comes from the
    restore job's own fresh preflight; the saved list is used only if none ran."""
    if not saved:return
    with _lock:
        if not _eligible:
            for m, tags in (saved.get('eligible') or {}).items():_eligible[m] = list(tags)
        for key, h in (saved.get('health') or {}).items():
            m, _, t = key.partition('|')
            _health[(m, t)] = dict(dict(fails=0, down_since=None, last_probe=0, calls=0, errors=0), **h)
        _log.extend(saved.get('log') or [])


def snapshot():
    with _lock:
        return dict(eligible=dict(_eligible), health={'%s|%s' % k: dict(v) for k, v in _health.items()}, log=list(_log[-200:]))


def reset():
    with _lock:
        _eligible.clear(); _health.clear(); _assign.clear(); del _log[:]


FAULT_FILE = os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data'))+'/live-config/provider-fault.json'
_fault_cache = dict(at=0, value={})


def injected_fault(model, tag, now=None):
    """Live fault injection for checks only: {"<model>": ["<provider>", ...]} makes calls whose
    first choice is a listed provider fail as HTTP 404 before sending. Re-read every 30 s."""
    if os.environ.get('V3_PRODUCTION_PROVIDER_FAULTS_DISABLED') == '1':return False
    now = time.time() if now is None else now
    if now - _fault_cache['at'] >= 30:
        try:_fault_cache['value'] = json.loads(open(FAULT_FILE).read())
        except (OSError, ValueError):_fault_cache['value'] = {}
        _fault_cache['at'] = now
    return tag in (_fault_cache['value'].get(model) or [])
