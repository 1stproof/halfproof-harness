"""Shared unmodified Qwen sandbox construction for probe and consolidated worker."""
import json
from pathlib import Path
from .control import atomic_json
from .research import prepare, CAS
BASE=Path(__file__).resolve().parents[1]
HOME=Path.home()
# turn-audit 2026-10-11: the board mount sets tools.eager to the board tools and read_file,
# and the pinned client defers every other eager-by-default built-in behind its
# tool_search/tool_call bridge. In p05, p06 and p01 every one of 1,116 write_file, edit and
# run_shell_command calls went through the bridge: 442 discovery calls and 53 calls refused
# for a missing tool name. Every brief requires NOTES.md and EXPERIMENTS.md, so research
# workers get these tools at startup (eager and visible), unless the arm disables them.
BRIDGED_CORE_TOOLS=('write_file','edit','run_shell_command')
def reveal_core_tools(settings):
    tools=settings.setdefault('tools',{})
    disabled=set(tools.get('disabled',[]))
    for name in BRIDGED_CORE_TOOLS:
        if name not in tools.get('core',[]) or name in disabled:continue
        for key in ('eager','visible'):
            if key=='eager' and 'eager' not in tools:continue  # no allowlist: nothing is deferred
            if name not in tools.setdefault(key,[]):tools[key].append(name)
    return settings
def command(config):
    role = getattr(config, 'role', 'research')
    watchdog = getattr(getattr(config, 'gateway', None), 'watchdog', None)
    if getattr(watchdog, 'state', {}).get('terminal') or getattr(watchdog, 'state', {}).get('frozen'):
        raise ValueError('terminal/frozen worker namespace refused')
    locked = bool(getattr(watchdog, 'state', {}).get('report_lock')) or bool(getattr(config, 'report_lock', False))
    # Same-model internal checking retains research tools before report lock.
    research_verifier = role in ('internal-verifier', 'verifier', 'reviewer', 'peer-reviewer') and watchdog is not None and not locked and not getattr(config, 'restricted_review', False)
    if role != 'research' and not research_verifier:
        from .role_sandbox import command as restricted_command
        return restricted_command(config)
    if locked:
        raise ValueError('research namespace refused after report lock')
    settings=json.loads((config.release/f'web-system-settings.{config.lane}.json').read_text())
    settings['mcpServers']['arxiv']['args']=['/harness/arxiv_entry.py']
    from .deployment import fixture_mounts, worker_fixtures, problem_mounts
    fixture_rows, deployment_mode = worker_fixtures(config)
    fixtures = fixture_mounts(fixture_rows, mode=deployment_mode)
    mounts,research_env=prepare(config)
    mounts += fixtures
    mounts += problem_mounts(config)
    from .tex import environment, install_template
    install_template(config.work)
    tex_mounts,tex_env=environment(config.work, config.release)
    mounts+=tex_mounts
    mounts += ['--ro-bind',str(config.work/'SOLUTION-TEMPLATE.tex'),'/work/SOLUTION-TEMPLATE.tex']
    settings['general']={'chatRecording':True}
    settings['model'].update(name=config.model,goalTokenBudget=-1,goalMaxTurns=-1,goalMaxActiveMinutes=-1,maxToolCalls=-1)
    provider=next(x for x in settings['modelProviders']['openai'] if x['id']==config.model)
    provider['baseUrl']='http://127.0.0.1:8877/v1'; provider['envKey']='SMOKE_API_KEY'
    from .provider_refusal import client_timing
    client_wall, client_timeout = client_timing(config)
    if client_wall is None:settings['model']['maxWallTimeSeconds'] = -1
    provider['generationConfig']['maxRetries']=0
    # Gateway buffers and owns bounded retries; local HTTP must outlive the incident.
    provider['generationConfig']['timeout']=client_timeout
    provider['generationConfig']['streamIdleTimeoutMs']=client_timeout
    provider['generationConfig']['streamMaxLifetimeMs']=client_timeout
    settings['modelProviders']['openai']=[provider]
    from .request_bound import apply_compaction
    apply_compaction(settings,provider,getattr(getattr(config,'gateway',None),'compact_tokens',None))
    settings['hooks']={'PostToolUse':[{'matcher':'*','hooks':[{'type':'command','command':'python3 /harness/hook.py'}]}]}
    if getattr(config, 'board_access', None) and config.board_access.get('arm_id')=='open-swarm' and config.lane=='glm':
        # Open-swarm light member: read and comment only, short turns (operator).
        settings['model']['maxToolCalls']=8
        settings.setdefault('tools',{})['disabled']=['run_shell_command','write_file','edit','web_fetch','web_search','agent','task_create','create_sub_session','notebook_edit','workflow','image_gen']
    if getattr(config, 'board_access', None):
        from .board_context import mount_client
        mounts += mount_client(config, settings)
    reveal_core_tools(settings)
    if config.scenario=='disabled-hook': settings['hooks']={}
    if config.scenario=='socket-unavailable':
     settings['hooks']['PostToolUse'][0]['hooks'][0]['command']='HARNESS_NOTICE_SOCKET=/harness-control/unavailable.sock python3 /harness/hook.py'
    atomic_json(config.control/'settings.json',settings)
    node=HOME/'.local/ds41-node'; qwen=HOME/'.local/qwen-npm'
    from .runtime_bundle import sources
    node_source, qwen_source = sources(config.release)
    bwrap=str(HOME/'.local/bin/bwrap')
    import shutil
    bwrap=shutil.which('bwrap') or bwrap
    from .scientific_bundle import executable
    bwrap=executable(config.release,'bwrap',bwrap)
    cmd=[bwrap,'--unshare-all','--die-with-parent','--new-session','--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64','--ro-bind','/etc','/etc','--proc','/proc','--dev','/dev','--tmpfs','/tmp','--tmpfs','/home','--dir',str(HOME),'--ro-bind',str(node_source),str(node),'--ro-bind',str(qwen_source),str(qwen),'--ro-bind',str(config.release),'/harness','--ro-bind',str(config.release/'forward.mjs'),'/forward.mjs','--ro-bind',str(config.control),'/harness-control','--bind',str(config.state),str(HOME/'.qwen'),'--bind',str(config.work),'/work','--chdir','/work','/bin/bash','-c','node /forward.mjs & exec "$@"','inner',str(qwen/'bin/qwen'),'--model',config.model,'--approval-mode','yolo','--input-format','stream-json','--output-format','stream-json','--max-tool-calls','-1']
    if client_wall is not None:cmd += ['--max-wall-time',client_wall]
    archive=config.work/'notes-archive'
    if archive.exists():
        if archive.is_symlink():raise ValueError('archive symlink refused')
        mounts+=['--ro-bind',str(archive),'/work/notes-archive']
    index=cmd.index('--chdir'); cmd[index:index]=mounts
    if getattr(config,'supervised',False):
        index=cmd.index('/bin/bash')
        cmd=cmd[:index]+['/usr/bin/python3','/harness/supervisor.py']+cmd[index+4:]
    env={'HOME':str(HOME),'PATH':str(node/'bin')+':/usr/bin:/bin','LANG':'C.UTF-8','TERM':'dumb','SMOKE_API_KEY':'fake-local-only','QWEN_CODE_SYSTEM_SETTINGS_PATH':'/harness-control/settings.json','DS_PROXY_UNIX':'/harness-control/api.sock','DS_PORT':'8877','QWEN_CODE_MAX_OUTPUT_TOKENS':str(config.gateway.admission.max_output_tokens if getattr(config,'gateway',None) is not None and getattr(config.gateway,'admission',None) else __import__('v2.output_limits',fromlist=['x']).provider_max(config.model)),'QWEN_USAGE_STATISTICS_ENABLED':'false',
         # Qwen 0.24.6 does not carry modelProviders generationConfig stream guards into the
         # pipeline (900 s lifetime default observed live); the env overrides are read directly.
         'QWEN_STREAM_MAX_LIFETIME_MS':str(client_timeout),'QWEN_STREAM_IDLE_TIMEOUT_MS':str(client_timeout),'QWEN_CODE_API_TIMEOUT_MS':str(client_timeout)}
    env.update(research_env)
    # The pinned POSIX entry already imports the CLI with gc exposed. In
    # stream-json mode the CLI otherwise retains that process and spawns a
    # second Node image, even inside our existing namespace. Keep one CLI
    # image; its heap uses Node's cgroup-aware default, with no reply/task cap.
    env['QWEN_CODE_NO_RELAUNCH']='true'
    env['PATH']=tex_env.pop('PATH')+':'+env['PATH']
    tex_env['LD_LIBRARY_PATH'] += ':'+env.get('LD_LIBRARY_PATH','')
    env.update(tex_env)
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    if getattr(config.gateway if hasattr(config,'gateway') else config, 'transport_metrics', False):
        env['DS_TRANSPORT_METRICS']=str(HOME/'.qwen/transport.jsonl')
    env['PATH']+=':/harness:'+str(CAS/'bin')
    env['QWEN_DEBUG_LOG_FILE']='1'
    return cmd,env
