"""Lenient repair of tool calls a model got slightly wrong (permissive design: never reject on format).

Live check 2026-10-07 (p02 rdcheck2): tool calls were refused although the intent was plain:
the arguments sent as a JSON text string instead of an object, and the tool name missing.
These helpers repair both; a call that cannot be repaired is passed through unchanged so the
original error still reaches the model.
"""
import json
try: from .format_json import loads
except ImportError: from format_json import loads

_NAME_KEYS = ('name', 'tool', 'tool_name', 'function', 'function_name')
_ARG_KEYS = ('arguments', 'args', 'params', 'parameters', 'input')


def parse_arguments(value):
    """An argument object from a dict or a JSON text string (possibly double-encoded); else None."""
    for _ in range(3):
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                value = loads(text)
            except (ValueError, SyntaxError):
                return None
            continue
        if value is None:
            return {}
        return None
    return dict(value) if isinstance(value, dict) else None


def _schema(tool):
    schema = tool.get('inputSchema') or tool.get('parameters') or (tool.get('function') or {}).get('parameters') or {}
    return set((schema.get('properties') or {})), set(schema.get('required') or [])


def _tool_name(tool):
    return tool.get('name') or (tool.get('function') or {}).get('name')


def infer_name(arguments, tools):
    """The one tool whose schema fits these argument keys (required present, nothing unknown)."""
    keys = set(arguments or {})
    fits = []
    for tool in tools or []:
        props, required = _schema(tool)
        if required <= keys and (not props or keys <= props) and (keys or not required):
            fits.append(_tool_name(tool))
    fits = [f for f in fits if f]
    return fits[0] if len(fits) == 1 else None


def repair_call(name, arguments, tools):
    """(name, arguments) with string arguments parsed and a missing name inferred when unique."""
    parsed = parse_arguments(arguments)
    if parsed is not None:
        arguments = parsed
    if name == 'tool_call' and isinstance(arguments, dict):
        # bridge call sent straight to a server: unwrap to the direct call it names
        inner_name = next((arguments[k] for k in _NAME_KEYS if isinstance(arguments.get(k), str) and arguments.get(k)), None)
        inner_raw = next((arguments[k] for k in _ARG_KEYS if k in arguments), None)
        inner = parse_arguments(inner_raw) if inner_raw is not None else (
            {k: v for k, v in arguments.items() if k not in _NAME_KEYS})
        if inner is not None:
            short = inner_name.split('__')[-1] if inner_name else None
            target = short if short in {_tool_name(t) for t in tools or []} else infer_name(inner, tools)
            if target:
                return target, inner
    if not name and isinstance(arguments, dict):
        name = infer_name(arguments, tools) or name
    return name, arguments


def repair_bridge_calls(calls, tools):
    """Accumulated streamed calls {index: {'name', 'arguments'(text)}}: unwrap a generic bridge call
    ('tool_call' {name, arguments}) aimed at a directly visible tool, parse string arguments, and
    infer a missing name. Returns the set of indices that were changed (calls edited in place)."""
    visible = {_tool_name(t): t for t in tools or [] if _tool_name(t)}
    changed = set()
    for index, call in calls.items():
        if call.get('server'):
            continue
        outer = parse_arguments(call.get('arguments'))
        name = call.get('name') or ''
        if name in ('tool_call', '') and isinstance(outer, dict):
            inner_name = next((outer[k] for k in _NAME_KEYS if isinstance(outer.get(k), str) and outer.get(k)), None)
            inner_raw = next((outer[k] for k in _ARG_KEYS if k in outer), None)
            if inner_name is not None or inner_raw is not None or name == '':
                inner = parse_arguments(inner_raw) if inner_raw is not None else (
                    {k: v for k, v in outer.items() if k not in _NAME_KEYS} if inner_name else dict(outer))
                if inner is None:
                    continue
                target = inner_name or infer_name(inner, list(visible.values()))
                if target in visible and target != 'tool_call':
                    call['name'] = target
                    call['arguments'] = json.dumps(inner)
                    changed.add(index)
                elif name == 'tool_call' and inner_name and inner_raw is not None and not isinstance(inner_raw, dict):
                    # bridge target not directly visible: keep the bridge, give it an object
                    call['arguments'] = json.dumps(dict({k: v for k, v in outer.items() if k not in _ARG_KEYS}, arguments=inner))
                    changed.add(index)
                continue
        if isinstance(call.get('arguments'), str):
            raw = call['arguments'].strip()
            try:
                once = loads(raw) if raw else {}
            except (ValueError, SyntaxError):
                continue
            try:
                strict = json.loads(raw) if raw else {}
            except ValueError:
                strict = None
            if isinstance(once, dict) and not isinstance(strict, dict):
                call['arguments'] = json.dumps(once); changed.add(index)
            if isinstance(once, str):
                again = parse_arguments(once)
                if again is not None:
                    call['arguments'] = json.dumps(again); changed.add(index)
    return changed
