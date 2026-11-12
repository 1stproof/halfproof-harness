"""Repair presentation only. Never invent fields, evidence, or dependency declarations."""
import ast
import json
import re
import io
import tokenize


def loads(value):
    if not isinstance(value, str):
        return value
    body = value.strip()
    if body.startswith('```'):
        body = re.sub(r'^```(?:json|javascript|python)?\s*\n?', '', body, flags=re.I)
        body = re.sub(r'\s*```$', '', body)
    try:
        return json.loads(body)
    except ValueError:
        pass
    # Only repair delimiters and literal control characters. Invalid escapes are
    # ambiguous: a mixed string may contain LaTeX whose prefixes are JSON escapes.
    out = []; quoted = False; i = 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            quoted = not quoted; out.append(ch)
        elif quoted and ch == '\\':
            nxt = body[i+1:i+2]
            valid = nxt in ('"', '\\', '/', 'b', 'f', 'n', 'r', 't') or (nxt == 'u' and re.fullmatch(r'[0-9a-fA-F]{4}', body[i+2:i+6]))
            if valid:
                out.append(body[i:i+2]); i += 1
            else:
                raise ValueError('Invalid JSON escape: encode literal backslashes as \\\\ (use a JSON serializer); do not change the content')
        elif quoted and ord(ch) < 32:
            out.append(json.dumps(ch)[1:-1])
        elif not quoted and ch == ',' and body[i+1:].lstrip().startswith(('}', ']')):
            pass
        else:
            out.append(ch)
        i += 1
    try:
        return json.loads(''.join(out))
    except ValueError:
        # Python literal notation is unambiguous and cannot execute code.
        # Python accepts unknown escapes and decodes known ones. Require raw
        # literals for scientific backslashes, or doubled backslashes / quotes.
        try:tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
        except tokenize.TokenError as exc:raise ValueError('Incomplete literal: send a complete JSON object') from exc
        for token in tokens:
            if token.type != tokenize.STRING: continue
            literal = token.string
            prefix = re.match(r"(?i)^([rubf]*)", literal)[1].lower()
            if 'r' in prefix: continue
            for match in re.finditer(r"\\(.)", literal, re.S):
                if match[1] not in ("\\", "'", '"'):
                    raise ValueError('Ambiguous literal escape: use JSON with doubled backslashes or a raw quoted literal; preserve the content')
        parsed = ast.literal_eval(body)
        if not isinstance(parsed, (dict, list, str, int, float, bool, type(None))):
            raise ValueError('expected JSON object, array, or scalar')
        return parsed


def refs(value):
    """Explicit record spellings only; absence is never converted to []."""
    if isinstance(value, str):
        body = value.strip().strip('`')
        body = re.sub(r'(?<=\])\s*[.;]$', '', body)
        try:
            value = loads(body)
        except (ValueError, SyntaxError):
            # Bare comma-separated numbers / board:N are presentation, not proof.
            if re.fullmatch(r'(?:board:|#)?[1-9]\d*(?:\s*,\s*(?:board:|#)?[1-9]\d*)*', body):
                value = body.split(',')
            elif body.startswith('[') and body.endswith(']'):
                items = body[1:-1].split(',')
                if not all(re.fullmatch(r'\s*(?:board:|#)?[1-9]\d*\s*', x) for x in items):
                    raise ValueError('refs requires an explicit array of revision IDs')
                value = items
            else:
                raise ValueError('refs requires an explicit array of revision IDs')
    if isinstance(value, list):
        return [int(re.sub(r'^(?:board:|#)', '', x.strip()))
                if isinstance(x, str) and re.fullmatch(r'(?:board:|#)?[1-9]\d*', x.strip()) else x for x in value]
    return value
