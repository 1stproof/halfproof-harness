"""Physical, arm-private board contexts. Controller secrets never enter mounts."""
import hashlib
import json
from pathlib import Path
from .control import atomic_json


def mount_client(config, settings):
    access = dict(config.board_access)
    socket_path = access.pop('socket')
    atomic_json(config.control/'board-client.json', access)
    (config.control/'board.sock').touch(exist_ok=False)
    settings.setdefault('mcpServers', {})['board'] = {
        'command':'/usr/bin/python3', 'args':['/harness/board_tools.py'],
        'trust':True, 'includeTools':['board_call','board_post']}
    settings.setdefault('tools', {}).setdefault('eager', []).extend(['mcp__board__board_call','mcp__board__board_post','read_file'])
    settings['tools'].setdefault('visible', []).extend(['mcp__board__board_call','mcp__board__board_post','read_file'])
    settings.setdefault('mcp', {}).setdefault('allowed', []).append('board')
    # Mount the socket inode only, never its parent, database or another token.
    return ['--ro-bind', str(socket_path), '/harness-control/board.sock']


def materialize_packet(packet, destination):
    """No truncation, author labels or peer verdicts; verify content addresses."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    artifacts = destination/'board-artifacts'; artifacts.mkdir()
    manifest = {k:v for k,v in packet.items() if k != 'evidence'}
    manifest['evidence'] = []
    total = 0
    for evidence in packet['evidence']:
        row = dict(evidence)
        content = row.pop('content')
        if content is not None:
            digest = hashlib.sha256(content).hexdigest()
            if digest != row['artifact_hash']:
                raise ValueError('board evidence content address mismatch')
            total += len(content)
            if total > 2*1024*1024:
                raise ValueError('essential packet exceeds bound')
            relative = 'board-artifacts/'+digest
            (destination/relative).write_bytes(content)
            row['path'] = relative
        manifest['evidence'].append(row)
    atomic_json(destination/'PACKET.json', manifest)
    return destination
