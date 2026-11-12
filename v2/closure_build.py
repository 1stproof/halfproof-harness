"""Deterministic report-only compiler. No provider client or source repair.

Run on compute, in P7's isolated publisher/build process with node-local scratch.
Only the lexical report dependency closure enters the no-network namespace.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import tex
from .owned import run_owned
from .closure_io import ClosureError, MAX_FILE, MAX_TOTAL, canonical, read_file, relative_name, sha256

FALLBACK = br'''\documentclass[12pt]{article}
\usepackage{fullpage}
\begin{document}
\section*{Administrative closure artifact}
This document is an administrative fallback, not a model-authored research
report. It makes no mathematical claim. The separate closure manifest records
the stop reason, authorship, available artifacts, and delivery status.
\end{document}
'''

# No dynamic path expansion: an unsupported include is a build failure, never
# an excuse to copy the worker's whole research directory.
REFERENCE = re.compile(r'\\(input|include|includegraphics|bibliography|addbibresource|bibliographystyle|usepackage|RequirePackage|documentclass)\s*(?:\[[^\]]*\]\s*)?\{([^{}]*)\}')
UNBRACED = re.compile(r'\\(?:input|include)\b\s*(?!\s*\{)\S+')
EXTENSIONS = {
    'input': ('.tex',), 'include': ('.tex',),
    'includegraphics': ('.pdf', '.png', '.jpg', '.jpeg', '.eps'),
    'bibliography': ('.bib',), 'addbibresource': ('.bib',),
    'bibliographystyle': ('.bst',), 'usepackage': ('.sty',),
    'RequirePackage': ('.sty',), 'documentclass': ('.cls',),
}
SYSTEM = {'usepackage', 'RequirePackage', 'documentclass', 'bibliographystyle'}


def report_dependencies(root, *, main='FINAL.tex'):
    relative_name(main)
    files = {main: read_file(root, main)}
    for name in ('CLAIMS-MANIFEST.json',):
        if (Path(root)/name).is_file():files[name]=read_file(root,name)
    try:
        files['PROVENANCE.md'] = read_file(root, 'PROVENANCE.md')
    except FileNotFoundError:
        pass
    pending = [main]
    while pending:
        current = pending.pop()
        if not current.endswith(('.tex', '.sty', '.cls')):
            continue
        text = tex.uncomment(files[current].decode('utf-8', errors='strict'))
        if UNBRACED.search(text):
            raise ClosureError('use braced literal report dependency paths')
        for match in REFERENCE.finditer(text):
            command, argument = match.groups()
            values = argument.split(',') if command in SYSTEM | {'bibliography'} else [argument]
            for value in values:
                value = value.strip()
                if any(char in value for char in '\\#{}~$'):
                    raise ClosureError('dynamic dependency path refused')
                relative_name(value)
                # TeX resolves ordinary input paths from its working directory.
                candidates = [value] if Path(value).suffix else [value + ext for ext in EXTENSIONS[command]]
                found = None
                for candidate in candidates:
                    if candidate == 'PROVENANCE.md':
                        raise ClosureError('PROVENANCE.md cannot be included in the mathematics paper')
                    if candidate in {'FINAL.pdf', 'bundle.json', 'latexmkrc', 'build-output'} or candidate.startswith('.tex-cache/'):
                        raise ClosureError('dependency collides with compiler or bundle output')
                    try:
                        data = read_file(root, candidate)
                    except FileNotFoundError:
                        continue
                    found = candidate
                    if candidate not in files:
                        files[candidate] = data
                        pending.append(candidate)
                    break
                if found is None and command not in SYSTEM:
                    raise ClosureError('missing report dependency: ' + value)
                if len(files) > 512 or sum(map(len, files.values())) > MAX_TOTAL // 2:
                    raise ClosureError('report dependency bound exceeded')
    # Detect changes spanning reads, in addition to per-descriptor checks.
    for name, data in files.items():
        if read_file(root, name) != data:
            raise ClosureError('report dependencies changed during copy')
    return files


def source_identity(root):
    """Exact dependency-closure identity, also usable on P2's verifier snapshot."""
    files = report_dependencies(root)
    entries = {name: sha256(data) for name, data in sorted(files.items())}
    return sha256(canonical(entries)), entries


@dataclass(frozen=True)
class BuildResult:
    passed: bool
    files: dict
    diagnostics: str
    metadata: dict

    @property
    def candidate_sha256(self):
        return sha256(canonical({k: sha256(v) for k, v in sorted(self.files.items()) if k != 'FINAL.pdf'}))


class ReportCompiler:
    """Pinned TeX engine, fixed arguments, 120-second aggregate invocation cap."""
    def __init__(self, scratch, *, timeout=120):
        if not 0 < timeout <= 120:
            raise ClosureError('compiler timeout must be within 120 seconds')
        self.scratch = Path(scratch)
        self.timeout = timeout

    def compile(self, root):
        try:
            files = report_dependencies(root)
        except (OSError, ValueError, UnicodeError) as exc:
            return BuildResult(False, {}, str(exc)[:4000], {'outcome': 'fail', 'error_class': 'dependency'})
        # Operator ruling 2026-10-06: formatting never blocks a report; deviations
        # from 12pt article + fullpage are recorded as warnings for normalisation
        # at submission. Only compilation and the page limit are hard limits.
        warnings = []
        for name, data in files.items():
            if name.endswith(('.tex', '.sty', '.cls')):
                violation = tex.layout_error(data, require_class=name == 'FINAL.tex')
                if violation:
                    warnings.append(name + ': ' + violation)
        metadata = {'outcome': 'fail', 'compiler': 'texlive/20250308', 'timeout_seconds': self.timeout,
                    'network': False, 'shell_escape': False, 'page_limit': tex.PAGE_LIMIT}
        if warnings:
            metadata['layout_warnings'] = warnings
        try:
            with tempfile.TemporaryDirectory(prefix='closure-build-', dir=self.scratch) as temp:
                stage = Path(temp)
                for name, data in files.items():
                    dest = stage / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                mounts, env = tex.environment(stage)
                (stage / 'latexmkrc').write_text("$pdflatex = 'pdflatex -no-shell-escape -recorder %O %S';\n")
                bwrap = shutil.which('bwrap') or str(Path.home() / '.local/bin/bwrap')
                from .scientific_bundle import executable
                bwrap=executable(os.environ.get('V3_SCIENTIFIC_RELEASE'),'bwrap',bwrap)
                command = [bwrap, '--unshare-all', '--die-with-parent', '--new-session',
                           '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
                           '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
                           '--ro-bind', '/etc', '/etc', '--proc', '/proc', '--dev', '/dev',
                           '--tmpfs', '/tmp', *mounts, '--bind', str(stage), '/work', '--chdir', '/work',
                           'latexmk', '-norc', '-r', 'latexmkrc', '-pdf', '-interaction=nonstopmode',
                           '-halt-on-error', 'FINAL.tex']
                # Bound individual engine/log output as well as runtime. No raw
                # provider credential or inherited environment enters the build.
                command = ['/usr/bin/prlimit', '--fsize=16777216:16777216', '--', *command]
                env.update(HOME='/tmp', LANG='C.UTF-8', SOURCE_DATE_EPOCH='0', FORCE_SOURCE_DATE='1')
                # TeX logs can contain engine/font bytes in a legacy encoding.
                # Decode diagnostics permissively; source and PDF bytes remain exact.
                with (stage / 'build-output').open('w+', encoding='utf-8', errors='replace') as out:
                    observer=getattr(self,'invocation_observer',None)
                    if observer:observer('start')
                    try:
                        proc = run_owned(command, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=self.timeout)
                    finally:
                        if observer:observer('end')
                    out.seek(0)
                    log = out.read(131072)
                try:
                    log += read_file(stage, 'FINAL.log').decode(errors='replace')[-131072:]
                except FileNotFoundError:
                    pass
                counts = re.findall(r'Output written on .*?\((\d+) pages?[,)]', log)
                if proc.returncode or not counts:
                    metadata['error_class'] = 'tex-error' if proc.returncode else 'page-count-unavailable'
                elif int(counts[-1]) > tex.PAGE_LIMIT:
                    metadata.update(error_class='page-limit', pages=int(counts[-1]))
                else:
                    pdf = read_file(stage, 'FINAL.pdf')
                    if not pdf.startswith(b'%PDF-'):
                        raise ClosureError('compiler did not produce a PDF')
                    metadata.update(outcome='pass', pages=int(counts[-1]), error_class=None)
                    return BuildResult(True, {**files, 'FINAL.pdf': pdf}, '', metadata)
                lines = [line for line in log.splitlines() if line.startswith('!') or 'Error' in line or 'l.' in line]
                return BuildResult(False, files, '\n'.join(lines[:12])[:4000] or log[-4000:], metadata)
        except subprocess.TimeoutExpired:
            metadata['error_class'] = 'timeout'
        except (OSError, ValueError) as exc:
            metadata['error_class'] = 'environment-or-output'
            return BuildResult(False, files, str(exc)[:4000], metadata)
        return BuildResult(False, files, 'Build exceeded its fixed deadline.', metadata)

    def fallback(self):
        with tempfile.TemporaryDirectory(prefix='closure-fallback-', dir=self.scratch) as temp:
            (Path(temp) / 'FINAL.tex').write_bytes(FALLBACK)
            return self.compile(temp)
