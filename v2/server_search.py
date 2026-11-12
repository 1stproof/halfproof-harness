"""Model-chosen, extractive Exa search. Never persist query/result telemetry.
OpenRouter web-search docs, Exa section: highlights are extractive excerpts
from the page; fast costs $0.007/search. Both uses and results are capped:
a result cap alone cannot bound searches that return zero results.
"""
import copy
import re

SEARCH_FEE = 7000  # microdollars per fast search, up to ten results
SEARCH_USES = 20  # raised from 1 (coordinator 18:3x PDT); fees counted when settled
SEARCH_BOUND = SEARCH_FEE * SEARCH_USES
SEARCH_TOOL = {'type':'openrouter:web_search', 'parameters':{
    'engine':'exa', 'mode':'fast', 'max_results':5, 'max_total_results':5,
    'max_uses':SEARCH_USES, 'max_characters':2000}}
SEARCH_SENTENCE = 'You can search the web and should use the arXiv tools to read a paper’s source.'

def configure(request, enabled):
    request['tools']=[t for t in request.get('tools',[]) if t.get('type')=='function']
    if enabled:
        request['tools'].append(copy.deepcopy(SEARCH_TOOL))
        request['max_tool_calls']=SEARCH_USES
    else:
        request.pop('max_tool_calls',None)

def search_count(usage):
    for key in ('server_tool_use_details','server_tool_use'):
        value=(usage or {}).get(key) or {}
        count=value.get('web_search_requests')
        if isinstance(count,int) and not isinstance(count,bool) and count>=0:return count
    return None

def host_matches(pinned, actual):
    # Generation records give display names, while routing uses endpoint slugs.
    def norm(s):return re.sub('[^a-z0-9]','',s.lower().split('/')[0])
    return norm(pinned)==norm(actual)

def scoped_tool_id(turn, index):
    """Fallback identity for a call without a provider ID."""
    import hashlib
    return "call_" + hashlib.sha256(str(turn).encode()).hexdigest()[:24] + "_" + str(index)


def replay_chunk(d, allowed_indices, call_ids=None, emitted_ids=None):
    """Allow only completion fields understood by Qwen; drop server events/citations.
    Classification uses fully accumulated function names, including fragmented names.
    Server indices never enter Qwen's function assembler; client indices are compacted.
    """
    if not isinstance(d.get('choices'),list):return None
    out={k:d[k] for k in ('id','object','created','model') if k in d}
    out['choices']=[]
    for choice in d['choices']:
        delta=choice.get('delta') or {}
        clean={k:delta[k] for k in ('role','content','reasoning','reasoning_content','reasoning_details') if k in delta}
        calls=[]
        for call in delta.get('tool_calls',[]):
            index=call.get('index',0)
            if index in allowed_indices:
                call=copy.deepcopy(call);call['index']=allowed_indices[index]
                if call_ids is not None:
                    if emitted_ids is None or index not in emitted_ids:
                        call['id']=call_ids[index]
                        if emitted_ids is not None:emitted_ids.add(index)
                    else:call.pop('id',None)
                calls.append(call)
        if calls:clean['tool_calls']=calls
        finish=choice.get('finish_reason')
        # Intermediate server-tool finishes are not a client tool boundary.
        if finish=='tool_calls' and not allowed_indices:finish=None
        out['choices'].append({'index':choice.get('index',0),'delta':clean,'finish_reason':finish})
    if d.get('usage'):
        out['usage']={k:d['usage'][k] for k in ('prompt_tokens','completion_tokens','total_tokens') if k in d['usage']}
    return out
