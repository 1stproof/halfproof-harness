"""Fetch authenticated notice from host memory, without persisting its nonce."""
import http.client, json, os, socket, sys
class UnixHTTP(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
  self.sock.settimeout(2); self.sock.connect(os.environ.get('HARNESS_NOTICE_SOCKET','/harness-control/api.sock'))
event=json.load(sys.stdin)
if event.get('hook_event_name')!='PostToolUse': print('{}')
else:
 c=UnixHTTP('localhost',timeout=2); c.request('GET','/notice'); r=c.getresponse()
 print(r.read().decode() if r.status==200 else '{}')
