"""Run the pinned legacy MCP implementation with content-free result telemetry."""
import importlib.util
import json
import os
from pathlib import Path
import uuid
from web_events import normalize

spec=importlib.util.spec_from_file_location('arxiv_mcp',Path(__file__).with_name('ds-arxiv-mcp.py'))
mcp=importlib.util.module_from_spec(spec);spec.loader.exec_module(mcp)
original=mcp.tool_result

def result(request_id,payload,is_error=False):
    event=normalize(payload,os.environ['DS_OPENALEX_LANE'],uuid.uuid4().hex,is_error)
    # Persist before delivery; host drains again after namespace teardown.
    with open(os.environ['DS_WEB_EVENTS'],'a') as out:
        out.write(json.dumps(event)+'\n');out.flush();os.fsync(out.fileno())
    original(request_id,payload,is_error)

mcp.tool_result=result
mcp.main()
