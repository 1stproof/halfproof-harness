"""Research parity inputs and CONNECT-only egress; never bypass model metering."""
import json
import os
from pathlib import Path
import select
import socket

HOME = Path.home()
BASE = Path(__file__).resolve().parents[1]
ALLOWLIST = ('api.openalex.org', 'arxiv.org', 'export.arxiv.org')
CAS = Path(os.environ.get('DS_CAS_ENV', os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data'))+'/cas/envs/sage')).resolve()
CACHE = Path(os.environ.get('DS_ARXIV_CACHE_DIR', os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data'))+'/arxiv-cache'))
INDEX = Path(os.environ.get('DS_ARXIV_LOCAL_INDEX', os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data'))+'/arxiv-meta/arxiv-metadata.sqlite3'))
PYTHON = HOME/'.local/lib/python3.12'

def description(lane):
    settings=json.loads((BASE/'bin'/f'web-system-settings.{lane}.json').read_text())
    return dict(tools=sorted(set(settings['tools']['core']+settings['tools']['visible'])),
                cas=str(CAS), cas_overlays=['checksum-verified PARI executable','checksum-verified Sage 10.9 package when missing'], cas_commands=['sage','gap','gp'], python=str(PYTHON),
                arxiv_cache=str(CACHE), arxiv_index=str(INDEX),
                research_allowlist=list(ALLOWLIST), model_egress='metered gateway only',
                openalex_lane=lane, openalex_daily_max=800, openalex_lane_share=160)

def prepare(config):
    """Private derived cache per run; only the fixed literature index is shared RO."""
    live=getattr(config,'research_live',False)
    telemetry=config.run/'research'; telemetry.mkdir(exist_ok=True)
    if live:
        cache,index=config.run/'arxiv-cache',INDEX
        cache.mkdir(exist_ok=True, mode=0o700)
        if cache.is_symlink() or not index.is_file(): raise ValueError('private cache/public index missing')
    else:
        cache=config.run/'arxiv-cache';cache.mkdir(exist_ok=True, mode=0o700)
        if cache.is_symlink(): raise ValueError('private cache symlink refused')
        index=config.control/'arxiv-test.sqlite3'
        if not index.exists():
            # Tiny synthetic record, no source downloaded and no shared cache access.
            import sqlite3
            with sqlite3.connect(index) as db:
                db.executescript("""CREATE TABLE papers(id,title,authors,abstract,categories,versions,update_date);
                CREATE VIRTUAL TABLE papers_fts USING fts5(id,title,authors,abstract,categories,content='papers',content_rowid='rowid');
                INSERT INTO papers VALUES('0000.00001','Synthetic arithmetic audit','Test Author','Euclidean algorithm gcd audit','math.NT','["v1"]','2026-10-03');
                INSERT INTO papers_fts(papers_fts) VALUES('rebuild');""")
            db.close()
    from .source_cache import shared_root
    watchdog = getattr(getattr(config, 'gateway', None), 'watchdog', None)
    source_cache = shared_root(watchdog) if watchdog is not None else config.run/'source-cache'
    source_cache.mkdir(parents=True, exist_ok=True)
    if source_cache.is_symlink(): raise ValueError('shared source cache symlink refused')
    from .scientific_bundle import bundle,mounts as scientific_mounts
    pinned=bundle(str(config.release))
    native_needed=('native-overlay' in pinned[1]['components'] if pinned else
        not (CAS/'lib/python3.12/site-packages/sage/all.py').exists())
    if pinned:
        mounts=scientific_mounts(config.release,research=True)
    else:
        for path in (CAS/'bin/sage',CAS/'bin/gap',PYTHON):
            if not path.exists(): raise ValueError('research dependency missing: '+str(path))
        mounts=['--ro-bind',str(CAS),str(CAS),'--ro-bind',str(PYTHON),str(PYTHON)]
    mounts+=['--ro-bind',str(index),'/research/index.sqlite3','--bind',str(cache),'/research/cache',
            '--bind',str(telemetry),'/research/events']
    if native_needed and not pinned:
        from .cas_overlay import sage_overlay, python_overlay, native_overlay
        overlay=sage_overlay(config.release.parent)
        python=python_overlay(config.release.parent,CAS)
        native=native_overlay(config.release.parent,CAS)
        mounts+=['--ro-bind',str(native/'lib'),'/research/cas-libs','--ro-bind',str(native/'share/gap'),str(CAS/'share/gap')]
        mounts+=['--ro-bind',str(native/'bin'),str(CAS/'bin')]
        for relative in ('share/singular','lib/singular'):
            if not (native/relative).is_dir():raise ValueError('private Singular data missing: '+relative)
            mounts+=['--ro-bind',str(native/relative),str(CAS/relative)]
        mounts+=['--ro-bind',str(CAS),'/research/original-cas','--ro-bind',str(overlay),'/research/sage',
                 '--ro-bind',str(python),str(CAS/'lib/python3.12/site-packages')]
    env={'PYTHONPATH':str(PYTHON/'site-packages'), 'DS_RUN_DIR':'/work','DS_CAS_ENV':str(CAS),
         'DS_SOURCE_CACHE_ENDPOINT':'http://127.0.0.1:8877/source-bytes', 'DS_ARXIV_CACHE_DIR':'/research/cache','DS_ARXIV_LOCAL_INDEX':'/research/index.sqlite3',
         'DS_ARXIV_CALL_CAP':os.environ.get('DS_ARXIV_CALL_CAP','10'),
         'DS_OPENALEX_LANE':config.lane,'DS_OPENALEX_DAILY_MAX':os.environ.get('DS_OPENALEX_DAILY_MAX','800'),
         'DS_OPENALEX_LANE_SHARE':os.environ.get('DS_OPENALEX_LANE_SHARE','160'),
         'DS_OPENALEX_BORROW_AFTER_UTC_HOUR':os.environ.get('DS_OPENALEX_BORROW_AFTER_UTC_HOUR','6'),
         'DS_ARXIV_USER_AGENT':os.environ.get('DS_ARXIV_USER_AGENT','ds41-math-research-harness/1.0 (model-free arXiv source audit)'),
         'QWEN_CODE_LEGACY_MCP_BLOCKING':'1', 'NODE_USE_ENV_PROXY':'1',
         'HTTPS_PROXY':'http://127.0.0.1:8877','HTTP_PROXY':'http://127.0.0.1:8877','NO_PROXY':'127.0.0.1,localhost',
         'DS_WEB_EVENTS':'/research/events/web-events.jsonl'}
    if native_needed:
        env['LD_LIBRARY_PATH']='/research/cas-libs:'+str(CAS/'lib')
    if live and not getattr(config,'preflight_only',False):
        key=HOME/'.config/ds41-secrets/openalex.key'
        env['DS_OPENALEX_API_KEY']=key.read_text().strip() if key.exists() else ''
    return mounts,env

def connect(handler, enabled=True):
    """Legacy allowlist except OpenRouter: its sole route is the metered API."""
    authority=handler.path.lower()
    if not enabled or authority not in {host+':443' for host in ALLOWLIST}:
        handler.send_error(403); return
    try:
        with socket.create_connection((authority[:-4],443),timeout=10) as upstream:
            handler.send_response(200,'Connection Established'); handler.end_headers()
            handler.wfile.flush()
            peers=(handler.connection,upstream)
            while not getattr(handler.server,'research_stopping',False):
                readable,_,_=select.select(peers,[],[],.25)
                for source in readable:
                    data=source.recv(65536)
                    if not data:return
                    (upstream if source is handler.connection else handler.connection).sendall(data)
    except OSError:
        return
