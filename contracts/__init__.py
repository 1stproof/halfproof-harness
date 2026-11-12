"""Pinned data and route authority; package identity prevents legacy-name shadowing."""


def __getattr__(name):
    # Old flattened executable releases import these prompt constants from a
    # neighboring contracts.py. Preserve that interface without route duplication.
    if name not in ('WRITEUP','AUDIT','BUILD'):
        raise AttributeError(name)
    try:
        from v2 import contracts as prompts
    except ModuleNotFoundError:
        import importlib.util
        from pathlib import Path
        source=Path(__file__).resolve().parent.parent/'contracts.py'
        spec=importlib.util.spec_from_file_location('_legacy_prompt_contracts',source)
        prompts=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prompts)
    return getattr(prompts,name)
