"""Single-writer content-addressed copy, pinned once per run."""
import hashlib, os, tempfile
from pathlib import Path

def pin_release(base, root):
    files = {p.name: p.read_bytes() for p in (base/'v2').iterdir()
             if p.is_file() and p.suffix in ('.py', '.cjs')}
    try: from .tex import manifest
    except ImportError: from tex import manifest
    import json
    from .runtime_bundle import pin_runtime
    from .startup_log import phase
    phase('runtime (node/qwen) pin start')
    runtime=pin_runtime(root)
    phase('runtime pin done')
    expected=base/'release/node-qwen-pin.json'
    if expected.is_file():
        if runtime!=json.loads(expected.read_bytes()):raise ValueError('Node/Qwen bytes differ from approved pin')
    elif (base/'release/scientific-dependencies.json.gz').is_file():
        raise ValueError('complete executable pin requires approved Node/Qwen identity')
    files['runtime-manifest.json'] = json.dumps(runtime,sort_keys=True).encode()
    scientific=base/'release/scientific-dependencies.json.gz'
    if scientific.is_file():
        from .scientific_bundle import pin,context
        pin(root,scientific)
        compiler_context=context(root,scientific)
        phase('scientific dependency pin done')
        files['scientific-manifest.json.gz']=scientific.read_bytes()
    files['tex-manifest.json']=json.dumps(manifest(),sort_keys=True).encode()
    files['SOLUTION-TEMPLATE.tex']=(base/'template/SOLUTION-TEMPLATE.tex').read_bytes()
    if (base/'contracts').is_dir():
        from .contracts_v3 import contracts_sha256
        files['contracts-manifest.json'] = json.dumps({'version':1, 'sha256':contracts_sha256(base/'contracts')},sort_keys=True).encode()
    for contract in sorted((base/'contracts').glob('*')):
        if contract.is_file() and contract.suffix in ('.json','.py'):
            files['contracts/'+contract.name] = contract.read_bytes()
    files['forward.mjs'] = (base/'bin/ds-loopback-forward.mjs').read_bytes()
    for path in [base/'bin/ds-arxiv-mcp.py', *(base/'bin').glob('web-system-settings.*.json')]:
        files[path.name]=path.read_bytes()
    if (base/'v2/cas_overlay.py').exists():
        # This environment's shared gp symlink is dangling; overlay only in v2.
        try: from .cas_overlay import gp_binary
        except ImportError: from cas_overlay import gp_binary
        files['gp-real']=gp_binary()
        files['gp']=b'#!/bin/sh\nexec env LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$DS_CAS_ENV/lib" /harness/gp-real -D "datadir=$DS_CAS_ENV/share/pari" "$@"\n'
    if (base/'CAMPAIGN-GOAL.txt').is_file():
        files['CAMPAIGN-GOAL.txt']=(base/'CAMPAIGN-GOAL.txt').read_bytes()
    digest = hashlib.sha256(b''.join(k.encode()+b'\0'+files[k] for k in sorted(files))).hexdigest()
    releases = root/'releases'
    releases.mkdir(exist_ok=True)
    target = releases/digest
    if target.exists():
        assert not target.is_symlink()
        assert all((target/k).read_bytes()==data for k,data in files.items())
        if scientific.is_file():os.environ['V3_SCIENTIFIC_RELEASE']=str(compiler_context)
        return target
    stage = Path(tempfile.mkdtemp(prefix='.copy-', dir=releases))
    for name, data in files.items():
        (stage/name).parent.mkdir(parents=True, exist_ok=True)
        (stage/name).write_bytes(data)
        (stage/name).chmod(0o555 if name in ('gp','gp-real') else 0o444)
    for directory in sorted((p for p in stage.rglob('*') if p.is_dir()), reverse=True):
        directory.chmod(0o555)
    stage.chmod(0o555)
    os.rename(stage, target)
    if scientific.is_file():os.environ['V3_SCIENTIFIC_RELEASE']=str(compiler_context)
    return target


def verify_release(release):
    """Bind the actual executable copy, not a caller-supplied version label."""
    import re
    release=Path(release)
    if release.is_symlink() or not re.fullmatch('[0-9a-f]{64}',release.name):
        raise ValueError('content-addressed runtime release required')
    files={}
    for path in release.rglob('*'):
        if path.is_symlink():raise ValueError('runtime release symlink refused')
        if path.is_file():files[str(path.relative_to(release))]=path.read_bytes()
    digest=hashlib.sha256(b''.join(k.encode()+b'\0'+files[k] for k in sorted(files))).hexdigest()
    if digest!=release.name:raise ValueError('runtime release executable bytes changed')
    return digest


def verify_controller_source(release):
    """Replacement must run the same controller modules as the pinned release.

    A code-changing upgrade requires a new Python controller process importing
    that source. Merely pointing an old object at a new worker release is refused.
    """
    import sys
    release=Path(release)
    for name,module in list(sys.modules.items()):
        if not name.startswith('v2.') or not getattr(module,'__file__',None):continue
        source=Path(module.__file__)
        if source.suffix!='.py':continue
        expected=release/source.name
        if not expected.is_file() or expected.read_bytes()!=source.read_bytes():
            raise ValueError('replacement controller module differs from pinned release: '+name)
    return verify_release(release)


def verify_dependency_pin(release, expected):
    """Compare actual private release identities with the independently signed pin."""
    import json
    from .scientific_bundle import read_manifest,verify_manifest,canonical
    release=Path(release)
    runtime=json.loads((release/'runtime-manifest.json').read_bytes())
    actual=dict(node_qwen_sha256=runtime['sha256'])
    if (release/'scientific-manifest.json.gz').is_file() or expected.get('scientific_sha256'):
        scientific=verify_manifest(read_manifest(release/'scientific-manifest.json.gz'))
        actual['scientific_sha256']=scientific['sha256']
    combined=hashlib.sha256(canonical(actual)).hexdigest()
    if any(actual[key]!=expected.get(key) for key in actual) or combined!=expected.get('dependency_manifest_sha256'):
        raise ValueError('private executable dependencies differ from accepted pin')
    return combined
