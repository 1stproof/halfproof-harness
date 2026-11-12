// Linux (rice) only. Runs INSIDE the bubblewrap sandbox, whose network namespace has only loopback.
// Listens on 127.0.0.1:$DS_PORT and pipes every connection to the allowlisting proxy's unix socket
// ($DS_PROXY_UNIX, bound in from outside). It adds no reachability: the proxy still tunnels only openrouter.ai:443.
import net from "node:net";
import fs from "node:fs";
const port = Number(process.env.DS_PORT || 8877), sock = process.env.DS_PROXY_UNIX || "/run/ds41/proxy.sock";
net.createServer((c) => {
  const u = net.connect(sock);
  c.pipe(u); u.pipe(c);
  if (process.env.DS_TRANSPORT_METRICS) {
    let pending = '', seen = false;
    u.on('data', (data) => {
      if (seen) return;
      pending += data.toString();
      const match = pending.match(/data: (.*)\n/);
      if (match) {
        seen = true;
        try { const d = JSON.parse(match[1]);
          fs.appendFileSync(process.env.DS_TRANSPORT_METRICS, JSON.stringify({id:d.id, first_sse:Number(process.hrtime.bigint())/1e9})+'\n');
        } catch {}
      }
      if (pending.length > 2*1024*1024) seen = true;
    });
  }
  u.on("error", () => c.destroy()); c.on("error", () => u.destroy());
}).listen(port, "127.0.0.1");
