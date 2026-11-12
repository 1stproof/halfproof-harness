"""Exclusive NFS-safe installation; staging evidence is retained."""
import json, os
from pathlib import Path

def fsync_path(p):
 fd=os.open(p,os.O_RDONLY|(os.O_DIRECTORY if Path(p).is_dir() else 0))
 try:os.fsync(fd)
 finally:os.close(fd)

def publish_noreplace(stage,target):
 """Copy-free publish of a fully fsynced stage tree into a NEW target directory."""
 stage=Path(stage);target=Path(target)
 os.mkdir(target,0o700)  # EEXIST if anything is there: never replaces
 for src in sorted(stage.rglob('*'),key=lambda p:len(p.parts)):
  dst=target/src.relative_to(stage)
  if src.is_symlink():raise RuntimeError('symlink in stage: '+str(src))
  if src.is_dir():os.mkdir(dst,0o700)
  else:os.link(src,dst)  # EEXIST if present: never replaces
 for p in sorted(target.rglob('*'),key=lambda p:len(p.parts),reverse=True):fsync_path(p)
 fsync_path(target);fsync_path(target.parent)

def verify_target(target,hashfile):
 m=json.loads((Path(target)/'RELEASE-MANIFEST.json').read_bytes())
 have={str(p.relative_to(target)) for p in Path(target).rglob('*') if p.is_file()}
 assert have==set(m['files'])|{'RELEASE-MANIFEST.json'},('file set mismatch',sorted(have^(set(m['files'])|{'RELEASE-MANIFEST.json'})))
 for n,h in m['files'].items():assert hashfile(Path(target)/n)==h,'hash mismatch '+n
 return len(m['files'])

def match_modes(stage,target):
 """Give target dirs the staging dirs' modes (release_files freezes some read-only), deepest first."""
 for p in sorted([p for p in Path(stage).rglob('*') if p.is_dir()],key=lambda p:len(p.parts),reverse=True):
  os.chmod(Path(target)/p.relative_to(stage),p.stat().st_mode&0o7777)
 os.chmod(target,Path(stage).stat().st_mode&0o7777);fsync_path(target)


def install(stage, target, hashfile):
    publish_noreplace(stage,target)
    verify_target(target,hashfile)
    match_modes(stage,target)
    return verify_target(target,hashfile)
