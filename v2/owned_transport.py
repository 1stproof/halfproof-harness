"""Reapable HTTP transport for an owned runtime session.

Accounting, request normalization and replay remain in P3 Gateway. Only blocking
network I/O lives in a child, so DNS/connect/header/body waits cannot outlive a
controller cancellation. Killing local transport never proves an upstream bill
is zero; Gateway retains the ordinary unresolved liability.
"""
import io
import multiprocessing
import threading
import time
import urllib.error
import urllib.request

from .gateway import Gateway


def _receive(pipe, url, raw, headers, timeout):
    try:
        request=urllib.request.Request(url,data=raw,headers=headers)
        with urllib.request.urlopen(request,timeout=timeout) as response:
            pipe.send(('headers',dict(response.headers)))
            while True:
                line=response.readline(16*1024*1024+1)
                if not line:break
                if len(line)>16*1024*1024:raise ValueError('oversized provider frame')
                pipe.send(('line',line))
        pipe.send(('end',None))
    except urllib.error.HTTPError as exc:
        pipe.send(('http-error',(exc.code,dict(exc.headers or {}),exc.read(1048576))))
    except (OSError,ValueError) as exc:
        # Never return request headers, credentials, or provider body diagnostics.
        pipe.send(('error',type(exc).__name__))
    finally:
        pipe.close()


class OwnedStream:
    def __init__(self,url,raw,headers,seconds,*,first_token_timeout=None):
        self.lock=threading.Lock();self.closed=False;self.headers={}
        self.silent_seconds=first_token_timeout;self.last_line=time.monotonic()  # set by the gateway (silent-request kill)
        self.deadline=time.monotonic()+max(.01,seconds)
        context=multiprocessing.get_context('spawn')
        self.pipe,child=context.Pipe(duplex=False)
        from .process import SpawnProcess
        self.process=SpawnProcess(target=_receive,args=(child,url,raw,headers,max(.01,first_token_timeout or seconds)),daemon=True)
        self.process.start();child.close()

    def __iter__(self):return self

    def __next__(self):
        while not self.closed and time.monotonic()<self.deadline:
            if self.silent_seconds is not None and time.monotonic()-self.last_line>=self.silent_seconds:
                raise TimeoutError('provider silent')
            try:
                if not self.pipe.poll(.1):continue
                kind,value=self.pipe.recv()
            except (EOFError,OSError):raise OSError('provider transport ended') from None
            if kind=='headers':self.headers=value;continue
            if kind=='line':return value
            if kind=='end':raise StopIteration
            if kind=='http-error':
                code,headers,body=value
                raise urllib.error.HTTPError('owned-provider',code,'provider error',headers,io.BytesIO(body))
            raise OSError('provider transport failed')
        raise OSError('provider transport cancelled or expired')

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            if self.process.is_alive():self.process.terminate()
            self.process.join(timeout=1)
            if self.process.is_alive():self.process.kill();self.process.join(timeout=1)
            if self.process.is_alive():raise RuntimeError('owned transport survived reap')
            self.pipe.close()


class RuntimeGateway(Gateway):
    """Per-session gateway; cancellation is irreversible for that session."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.transport_lock=threading.Lock();self.cancelled=False
        self.transports=[];self.active_handlers=0

    def handle(self,handler,request,worker):
        with self.transport_lock:
            if self.cancelled:handler.send_error(503);return
            self.active_handlers+=1
        try:return super().handle(handler,request,worker)
        finally:
            with self.transport_lock:
                self.active_handlers-=1
                # Closed Process objects otherwise retain their sentinel fds for
                # this gateway's lifetime. Keep only genuinely owned live I/O.
                self.transports[:]=[stream for stream in self.transports if not stream.closed]

    def _open(self,request,worker,rid,raw):
        if self.fake is not None:return super()._open(request,worker,rid,raw)
        return self.open_transport('https://openrouter.ai/api/v1/chat/completions',raw,
            {'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})

    def open_transport(self,url,raw,headers):
        with self.transport_lock:
            if self.cancelled:raise OSError('session cancelled')
            deadline=min(self.watchdog.run.start+self.watchdog.run.wall,
                         self.watchdog.state.get('request_deadline',float('inf')))
            from .gateway import first_token_seconds
            stream=OwnedStream(url,raw,headers,deadline-self.watchdog.clock(),
                               first_token_timeout=first_token_seconds(self.model))
            self.transports.append(stream)
            return stream

    def cancel_worker(self,worker):
        with self.transport_lock:
            self.cancelled=True
            for stream in self.transports:stream.close()
