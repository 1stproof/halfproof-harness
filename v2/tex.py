"""Pinned offline TeX environment and bounded, copy-only build sandbox."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
try: from .owned import run_owned
except ImportError: from owned import run_owned

PREFIX=Path(os.environ.get('HARNESS_TEX_PREFIX','/software/spack/opt/spack/linux-x86_64_v3'))
TREE=PREFIX/'texlive-20250308-mugsoo5c7d2dcnsj224btfshfnszi6wj'
LIBS=[TREE/'lib']+[PREFIX/(name+'/lib') for name in (
    'gcc-runtime-13.3.0-74pp53wkiwh6vcw5rsr7inpkln74cc2p',
    'libpng-1.6.47-h3hy6g5mxfmxrmyvx3adgswttzp4swer',
    'zlib-ng-2.2.4-ie7i656z66ppyc74p7wl77u6a5rwek2z')]
COMMAND='latexmk -pdf -interaction=nonstopmode -halt-on-error FINAL.tex'
try: from .settings import PAGE_LIMIT
except ImportError: from settings import PAGE_LIMIT

def template_bytes():
    here=Path(__file__).resolve().parent
    path=here/'SOLUTION-TEMPLATE.tex'
    if not path.is_file():path=here.parent/'template/SOLUTION-TEMPLATE.tex'
    return path.read_bytes()

def install_template(work):
    target=Path(work)/'SOLUTION-TEMPLATE.tex'
    if target.is_symlink():raise ValueError('template symlink refused')
    if target.is_file() and target.read_bytes()==template_bytes():return
    if target.exists():target.chmod(0o644)
    target.write_bytes(template_bytes());target.chmod(0o444)

# Use exactly the submission template for launch preflight. The test sentence
# also provides a stable insertion point for synthetic compiler-fault tests.
FIXED=template_bytes().decode().replace(r'\end{document}',
    'Offline preflight: $1+1=2$.\n'+r'\end{document}')

def uncomment(text):
    return re.sub(r'(?<!\\)%[^\n]*', '', text)

def layout_error(source, require_class=True):
    text=uncomment(source.decode('utf-8',errors='replace'))
    classes=re.findall(r'\\documentclass\s*(?:\[([^]]*)\])?\s*\{([^}]*)\}',text)
    classes=[(options.strip(),name.strip()) for options,name in classes]
    if require_class and classes != [('12pt','article')]:
        return r'Use exactly \documentclass[12pt]{article}.'
    if not require_class and classes:return 'Declare the document class only in FINAL.tex.'
    for options,packages in re.findall(r'\\(?:usepackage|RequirePackage)\s*(?:\[([^]]*)\])?\s*\{([^}]*)\}',text):
        packages={x.strip() for x in packages.split(',')}
        if packages & {'geometry','setspace','anysize','typearea','savetrees','extsizes','relsize','moresize','anyfontsize','scalefnt'}:
            return 'Margin, layout, and line-spacing packages are forbidden.'
        if 'fullpage' in packages and options.strip():return 'Only plain fullpage without options is permitted.'
    if re.search(r'\\(?:geometry|linespread|baselinestretch|newgeometry|restoregeometry|fontsize|relsize|smaller|larger|textsmaller|textlarger|scalefont|setstretch|tiny|scriptsize|footnotesize|small|large|Large|LARGE|huge|Huge|normalsize|baselineskip|singlespacing|onehalfspacing|doublespacing|mag|pdfpagewidth|pdfpageheight)\b',text):
        return 'Do not change body font size, line spacing, or page layout.'
    dimensions=r'(?:textwidth|textheight|oddsidemargin|evensidemargin|topmargin|headheight|headsep|footskip|marginparwidth|marginparsep|paperwidth|paperheight|hoffset|voffset|columnsep|columnwidth|linewidth|parskip|parindent)'
    if re.search(r'\\(?:setlength|addtolength)\s*\{?\s*\\'+dimensions+r'\b',text) or re.search(r'\\'+dimensions+r'\s*(?:=|[-+0-9])',text):
        return 'Do not alter text, margin, or paper dimensions.'
    if re.search(r'\\(?:advance|multiply|divide)\s*\\'+dimensions+r'\b',text):
        return 'Do not alter text, margin, or paper dimensions.'
    if re.search(r'\\(?:PassOptionsToPackage|PassOptionsToClass|LoadClass|documentstyle|enlargethispage|addtoplength|setspace|font|fontdimen|textfont|scriptfont|scriptscriptfont|catcode|csname)\b|\^\^',text):
        return 'Indirect layout/font overrides and dynamic control names are not permitted.'
    return None


def manifest():
    return {'version':'texlive/20250308','tree':str(TREE),'libraries':[str(p) for p in LIBS],
            'binaries':{n:hashlib.sha256((TREE/'bin/x86_64-linux'/n).read_bytes()).hexdigest()
                        for n in ('pdflatex','latexmk','bibtex','kpsewhich')}}

def environment(work, release=None):
    cache=Path(work)/'.tex-cache'
    if cache.is_symlink():raise ValueError('TeX cache symlink refused')
    cache.mkdir(exist_ok=True)
    mounts=[]
    for p in [TREE,*LIBS]:
        if not p.is_dir():raise FileNotFoundError('pinned TeX environment missing')
        mounts+=['--ro-bind',str(p),str(p)]
    pinned_release=release or os.environ.get('V3_SCIENTIFIC_RELEASE')
    if pinned_release:
        try: from .scientific_bundle import mounts as scientific_mounts
        except ImportError: from scientific_bundle import mounts as scientific_mounts
        mounts+=scientific_mounts(pinned_release)
    return mounts,dict(PATH=str(TREE/'bin/x86_64-linux')+':/usr/bin:/bin',
        LD_LIBRARY_PATH=':'.join(map(str,LIBS)),TEXMFVAR='/work/.tex-cache',
        TEXMFCACHE='/work/.tex-cache',shell_escape='f',openout_any='p',openin_any='p')

def dependencies(work):
    """Bound enumeration as well as bytes; never descend through symlinks."""
    try: from .artifacts import EXCLUDED
    except ImportError: from artifacts import EXCLUDED
    pending=[(Path(work),0)];visited=0
    while pending:
        directory,depth=pending.pop()
        if depth>16:raise ValueError('dependency-depth')
        with os.scandir(directory) as entries:
            for entry in entries:
                visited+=1
                if visited>4096:raise ValueError('dependency-count')
                if entry.name.startswith('.') or entry.name in EXCLUDED or entry.name=='notes-archive':continue
                # Downloaded paper sources are research inputs, not parts of the report.
                if depth==0 and entry.name=='literature':continue
                path=Path(entry.path);relative=path.relative_to(work)
                if entry.is_dir(follow_symlinks=False):pending.append((path,depth+1));continue
                if not entry.is_file(follow_symlinks=False):continue
                if relative in (Path('FINAL.tex'),Path('FINAL.pdf')):continue
                if path.suffix in ('.tex','.bib','.sty','.cls','.png','.jpg','.pdf','.bst','.csv','.dat','.eps','.svg'):
                    yield path,relative

def input_signature(work):
    """Hash dependency metadata without exporting private filenames."""
    digest=hashlib.sha256()
    try:
        for path,relative in sorted(dependencies(work),key=lambda item:str(item[1])):
            stat=path.stat(follow_symlinks=False)
            digest.update(str((str(relative),stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)).encode())
    except (OSError,ValueError):return None
    return digest.hexdigest()

def build(work, source=None):
    """Only scalar result leaves telemetry; diagnostics are private model context."""
    try: from .artifacts import report_content
    except ImportError: from artifacts import report_content
    work=Path(work).resolve()
    source=report_content(work,'FINAL.tex') if source is None else source
    digest=hashlib.sha256(source).hexdigest()
    result={'outcome':'fail-error-class','error_class':'missing-report','sha256':digest,'inputs_sha256':input_signature(work)}
    if not source:return result,'FINAL.tex is missing or unreadable.'
    violation=layout_error(source)
    if violation:result['layout_warning']=violation  # warning only (operator ruling 2026-10-06)
    try:
        mounts,env=environment(work)
        with tempfile.TemporaryDirectory(prefix='tex-build-',dir=Path(work).parent) as directory:
            stage=Path(directory);(stage/'FINAL.tex').write_bytes(source)
            (stage/'latexmkrc').write_text("$pdflatex = 'pdflatex -no-shell-escape %O %S';\n")
            # Copy bounded regular local dependencies, never follow links or overwrite FINAL.
            total=len(source)
            for path,relative in dependencies(work):
                data=report_content(path.parent,path.name,16*1024*1024)
                total+=len(data)
                if total>64*1024*1024:raise ValueError('dependency-size')
                if path.suffix in ('.tex','.sty','.cls') and relative != Path('SOLUTION-TEMPLATE.tex'):
                    violation=layout_error(data,require_class=False)
                    if violation:result.setdefault('layout_warnings',[]).append(str(relative)+': '+violation)
                target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            if result['inputs_sha256'] is None or result['inputs_sha256']!=input_signature(work):
                result['error_class']='source-changed';return result,'Build inputs changed during copying; retry.'
            bwrap=shutil.which('bwrap') or str(Path.home()/'.local/bin/bwrap')
            from .scientific_bundle import executable
            bwrap=executable(os.environ.get('V3_SCIENTIFIC_RELEASE'),'bwrap',bwrap)
            cmd=[bwrap,'--unshare-all','--die-with-parent','--new-session','--ro-bind','/usr','/usr',
                 '--symlink','usr/bin','/bin','--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64',
                 '--ro-bind','/etc','/etc','--proc','/proc','--dev','/dev','--tmpfs','/tmp',*mounts,
                 '--bind',str(stage),'/work','--bind',str(Path(work)/'.tex-cache'),'/work/.tex-cache',
                 '--chdir','/work','latexmk','-norc','-r','latexmkrc','-pdf','-interaction=nonstopmode','-halt-on-error','FINAL.tex']
            env.update(HOME='/tmp',LANG='C.UTF-8')
            # File output avoids unbounded pipe buffering; timeout kills the namespace owner.
            with (stage/'build-output').open('w+',errors='replace') as out:
                proc=run_owned(cmd,env=env,stdout=out,stderr=subprocess.STDOUT,timeout=120)
                out.seek(0);log=out.read(131072)
            if proc.returncode==0 and (stage/'FINAL.pdf').is_file():
                texlog=log
                if (stage/'FINAL.log').is_file():
                    # The summary is at the end; bound memory even for noisy TeX.
                    with (stage/'FINAL.log').open('rb') as stream:
                        stream.seek(0,os.SEEK_END)
                        stream.seek(max(0,stream.tell()-131072))
                        texlog=stream.read(131072).decode(errors='replace')
                counts=re.findall(r'Output written on .*?\((\d+) pages?[,)]',texlog)
                if not counts:
                    result['error_class']='page-count-unavailable'
                    return result,'Cannot verify the page count; rebuild FINAL.tex without changing the layout.'
                pages=int(counts[-1]);result.update(pages=pages,page_limit=PAGE_LIMIT)
                if pages>PAGE_LIMIT:
                    result['error_class']='page-limit'
                    return result,f'Page limit failed: {pages} pages; maximum {PAGE_LIMIT}. Shorten the exposition; never change the layout.'
                result.update(outcome='pass',error_class=None);return result,''
            missing=bool(re.search(r"(?:LaTeX Error: File .* not found|I can't find file|I couldn't open (?:database|style) file|command not found|error while loading shared libraries|bwrap:)",log))
            result.update(outcome='environment-missing' if missing else 'fail-error-class',error_class='dependency-missing' if missing else 'tex-error')
            lines=[line for line in log.splitlines() if line.startswith('!') or 'Error' in line or 'not found' in line]
            return result,'\n'.join(lines[:8])[:4000] or '\n'.join(log.splitlines()[:8])[:4000]
    except subprocess.TimeoutExpired:
        result.update(error_class='timeout');return result,'TeX build exceeded 120 seconds.'
    except (OSError,ValueError):
        result.update(outcome='environment-missing',error_class='environment-unavailable');return result,'Pinned build environment or local dependency unavailable.'

def preflight(work):
    install_template(work)
    result,_=build(work,FIXED.encode())
    if result['outcome']!='pass':raise RuntimeError('TeX launch preflight failed: '+result['error_class'])
    return result
