"""Optional whole-key backstop; its delta is NEVER charged to a run ledger."""
import json, threading, time, urllib.request
class KeyUsage:
    def __init__(self,key):
        self.baseline=None;self.delta=0;self.status='pending';self.stop=threading.Event()
        def poll():
            while not self.stop.is_set():
                try:
                    req=urllib.request.Request('https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+key})
                    with urllib.request.urlopen(req,timeout=5) as response:value=float(json.load(response)['data']['usage'])
                    if self.baseline is None:self.baseline=value
                    self.delta=max(0,int((value-self.baseline)*1000000));self.status='ok'
                except Exception:self.status='unavailable'
                self.stop.wait(60)
        self.thread=threading.Thread(target=poll,daemon=True);self.thread.start()
    def close(self):self.stop.set()
