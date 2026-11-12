"""Restricted Qwen role namespaces with service-enforced bounded file tools."""
import json
import os
from pathlib import Path
import shutil
from .control import atomic_json
from .deployment import DeploymentRefused, fixture_mounts, worker_fixtures
from .role_tools import ROLES, tools_for


def command(config):
    role = {'report': 'report-author', 'verifier': 'internal-verifier', 'reviewer': 'peer-reviewer'}.get(config.role, config.role)
    if role == 'specialist' and config.lane not in ('k3', 'qwen24t', 'glm', 'dsp'):
        raise DeploymentRefused('specialist role requires a declared opening lane')
    if role not in ROLES:
        raise DeploymentRefused('unsupported restricted role')
    if not hasattr(config, 'role_evidence'):
        from .evidence_snapshot import copy_evidence
        config.role_evidence = Path(config.run)/'role-evidence'
        copy_evidence(config.work, config.role_evidence)
    if not hasattr(config, 'role_output'):
        config.role_output = config.work
    evidence = Path(config.role_evidence).resolve(strict=True)
    output = Path(config.role_output).resolve(strict=True)
    state = Path(config.state).resolve(strict=True)
    if any(state.iterdir()):
        raise DeploymentRefused('restricted role requires fresh client state')
    if not all(x.is_dir() for x in (evidence, output, state)):
        raise DeploymentRefused('role directories required')
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for a, b in ((evidence, output), (evidence, state), (output, state))):
        raise DeploymentRefused('evidence, output and session roots must be disjoint')
    if role == 'specialist' and any(output.iterdir()):
        raise DeploymentRefused('specialist has no output capability')
    settings = json.loads((config.release/f'web-system-settings.{config.lane}.json').read_text())
    provider = next(x for x in settings['modelProviders']['openai'] if x['id'] == config.model)
    provider.update(baseUrl='http://127.0.0.1:8877/v1', envKey='SMOKE_API_KEY')
    from .provider_refusal import client_timing
    client_wall, client_timeout = client_timing(config)
    if client_wall is None:settings['model']['maxWallTimeSeconds'] = -1
    provider['generationConfig'].update(maxRetries=0, timeout=client_timeout, streamIdleTimeoutMs=client_timeout, streamMaxLifetimeMs=client_timeout)
    settings['modelProviders']['openai'] = [provider]
    from .request_bound import apply_compaction
    apply_compaction(settings, provider, getattr(getattr(config, 'gateway', None), 'compact_tokens', None))
    settings['model'].update(name=config.model, goalMaxTurns=-1, goalTokenBudget=-1, goalMaxActiveMinutes=-1)
    names = [x['name'] for x in tools_for(role)]
    settings['tools'] = {'core': [], 'visible': ['mcp__role_files__'+x for x in names],
                         'disabled': ['run_shell_command', 'exec', 'web_fetch', 'web_search', 'read_file', 'write_file', 'edit', 'glob', 'grep_search', 'agent', 'advisor', 'skill', 'tool_call', 'tool_search', 'create_goal', 'propose_goal', 'get_goal', 'update_goal', 'list_agents', 'create_sub_session', 'send_message', 'team_create', 'task_create', 'task_stop', 'ask_user_question', 'monitor', 'cron_create', 'loop_wakeup', 'enter_plan_mode', 'exit_plan_mode', 'read_mcp_resource', 'notebook_edit', 'workflow', 'save_memory', 'image_gen']}
    settings['mcpServers'] = {'role_files': {'command': '/usr/bin/python3', 'args': ['/harness/role_tools.py'], 'trust': True, 'includeTools': names}}
    settings['mcp'] = {'allowed': ['role_files']}
    if role == 'orchestrator':
        settings['mcpServers']={}
        settings['mcp']={'allowed': []}
    settings['hooks'] = {}
    settings['tools']['eager'] = ['mcp__role_files__'+x for x in names]
    atomic_json(config.control/'settings.json', settings)
    home = Path.home()
    node, qwen = home/'.local/ds41-node', home/'.local/qwen-npm'
    from .runtime_bundle import sources
    node_source, qwen_source = sources(config.release)
    bwrap = shutil.which('bwrap') or str(home/'.local/bin/bwrap')
    from .scientific_bundle import executable
    bwrap=executable(config.release,'bwrap',bwrap)
    cmd = [bwrap, '--unshare-all', '--die-with-parent', '--new-session',
           '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
           '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
           '--ro-bind', '/etc', '/etc', '--proc', '/proc', '--dev', '/dev',
           '--tmpfs', '/tmp', '--tmpfs', '/home', '--dir', str(home),
           '--ro-bind', str(node_source), str(node), '--ro-bind', str(qwen_source), str(qwen),
           '--ro-bind', str(config.release), '/harness',
           '--ro-bind', str(config.release/'forward.mjs'), '/forward.mjs',
           '--ro-bind', str(config.control), '/harness-control',
           '--bind', str(state), str(home/'.qwen'), '--ro-bind', str(evidence), '/evidence',
           '--dir', '/work']
    if role != 'specialist':
        cmd += ['--bind', str(output), '/role-output']
    fixture_rows, deployment_mode = worker_fixtures(config)
    cmd += fixture_mounts(fixture_rows, mode=deployment_mode)
    from .deployment import problem_mounts
    cmd += problem_mounts(config)
    # Restricted roles need the pinned system interpreter/runtime only. Never
    # mount scientific tools or writable research caches into these namespaces.
    from .scientific_bundle import bundle
    pinned=bundle(str(config.release))
    if pinned:
        root,manifest=pinned
        for label in ('system','system-fonts','system-alternatives'):
            if label in manifest['components']:
                cmd+=['--ro-bind',str(root/label),manifest['components'][label]['target']]
    cmd += ['--chdir', '/work', '/usr/bin/python3', '/harness/supervisor.py',
            str(qwen/'bin/qwen'), '--model', config.model, '--approval-mode', 'yolo',
            '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--max-tool-calls', '-1']
    if client_wall is not None:cmd += ['--max-wall-time',client_wall]
    env = {'HOME': str(home), 'PATH': str(node/'bin')+':/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'HARNESS_EVIDENCE_READ_LIMIT': '16' if getattr(config,'report_reads_restricted',False) else '',
           'QWEN_CODE_DISABLE_CRON': '1', 'QWEN_CODE_DISABLE_ARTIFACT': '1',
           'TERM': 'dumb', 'HARNESS_ROLE': role, 'SMOKE_API_KEY': 'fake-local-only',
           'QWEN_CODE_SYSTEM_SETTINGS_PATH': '/harness-control/settings.json',
           'DS_PROXY_UNIX': '/harness-control/api.sock', 'DS_PORT': '8877',
           'QWEN_STREAM_MAX_LIFETIME_MS': str(client_timeout), 'QWEN_STREAM_IDLE_TIMEOUT_MS': str(client_timeout),
           'QWEN_CODE_MAX_OUTPUT_TOKENS': str(config.gateway.admission.max_output_tokens) if getattr(config,'gateway',None) and config.gateway.admission else str(__import__('v2.output_limits',fromlist=['x']).provider_max(config.model)),
           'QWEN_CODE_API_TIMEOUT_MS': str(client_timeout), 'QWEN_USAGE_STATISTICS_ENABLED': 'false',
           'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
    return cmd, env
