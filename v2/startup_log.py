"""Timestamped start-up phase lines on stderr (the job's .slurm.log)."""
import os
import sys
import time


def phase(message):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
    print('[startup] %s pid=%d %s' % (stamp, os.getpid(), message), file=sys.stderr, flush=True)
