"""Explicit root authorization; never authorize the legacy live runs tree."""
import json
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]

DEFAULT_CAMPAIGN_ROOT = Path.home()/'oss-harness-data/runs-v2'

def campaign_root(data):
    return data.get('runs_root', str(DEFAULT_CAMPAIGN_ROOT))

def canonical(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink/traversal refused')
    return path

def validate_root(root, name, manifest=None):
    root = canonical(root)
    legacy = BASE/'runs'
    if root == legacy or legacy in root.parents:
        raise ValueError('legacy live runs tree refused')
    if not name or '/' in name or name in ('.', '..'):
        raise ValueError('run name refused')
    if root != BASE/'runs-v2-test':
        if manifest is None: raise ValueError('campaign manifest required')
        data = json.loads(canonical(manifest).read_text())
        if data.get('version') != 1 or data.get('authorization') != 'operator-approved-campaign-root' or canonical(campaign_root(data)) != root:
            raise ValueError('campaign root not authorized')
    canonical(root/name)
    return root
