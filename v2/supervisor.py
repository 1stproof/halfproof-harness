"""Namespace-local supervisor: distinguish CLI exit from supervisor loss.
No restart/budget policy here; the host watchdog owns those decisions.
"""
import json, os, signal, subprocess, sys
from pathlib import Path

forward=subprocess.Popen(['node','/forward.mjs'])
worker=None
stopping=False

def stop(*_):
    global stopping
    stopping=True
    if worker is not None and worker.poll() is None:worker.terminate()
    if forward.poll() is None:forward.terminate()

signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)
try:
    worker=subprocess.Popen(sys.argv[1:])
    code=worker.wait()
    path=Path(os.environ['HOME'])/'.qwen'/'worker-exit.json'
    stage=path.with_suffix('.tmp')
    stage.write_text(json.dumps({'exit_code':code}));os.replace(stage,path)
finally:
    stop()
    try:forward.wait(timeout=1)
    except subprocess.TimeoutExpired:forward.kill();forward.wait()
sys.exit(0)
