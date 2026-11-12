"""Normalize every forwarded request to the lane's own settings.

Qwen Code's side queries (goal verifier, classifiers) send temperature 0, reasoning off
and a JSON schema. The measurement rule requires the manufacturer's default sampling,
and pinned hosts refuse some of these under require_parameters (measured 2026-10-03:
Moonshot mxfp4 404s on temperature/top_p/seed/parallel_tool_calls; Z.AI fp8 404s on
json_schema and 400s on reasoning effort none; Alibaba 400s on reasoning effort none).
"""
import json
from pathlib import Path

SAMPLING = ('temperature','top_p','top_k','min_p','top_a','seed','frequency_penalty',
            'presence_penalty','repetition_penalty','logit_bias','parallel_tool_calls')
REASONING_FIELDS = ('reasoning_effort','enable_thinking','thinking','thinking_budget',
                    'chat_template_kwargs','include_reasoning')
NO_JSON_SCHEMA = {'z-ai/glm-5.3'}
SETTINGS = Path(__file__).resolve().parents[1]/'bin/web-system-settings.json'

def efforts():
    data = json.loads(SETTINGS.read_text())
    return {e['id']: e['generationConfig']['reasoning']
            for e in data['modelProviders']['openai'] if e.get('generationConfig', {}).get('reasoning')}

EFFORTS = efforts()

def normalize(request, model):
    for key in SAMPLING + REASONING_FIELDS:
        request.pop(key, None)
    if model in EFFORTS:  # test fixtures use synthetic model names
        request['reasoning'] = dict(EFFORTS[model])
    fmt = request.get('response_format')
    if isinstance(fmt, dict) and fmt.get('type') == 'json_schema' and model in NO_JSON_SCHEMA:
        schema = (fmt.get('json_schema') or {}).get('schema')
        request['response_format'] = {'type': 'json_object'}
        note = 'Reply with one JSON object matching this JSON Schema: ' + json.dumps(schema, sort_keys=True)
        messages = request.setdefault('messages', [])
        if messages and messages[0].get('role') == 'system' and isinstance(messages[0].get('content'), str):
            messages[0]['content'] += '\n\n' + note
        else:
            messages.insert(0, {'role': 'system', 'content': note})
    return request
