#!/usr/bin/env python3
"""hookcatch: catch every webhook on localhost and inspect it live.

    python hookcatch.py            # listens on 127.0.0.1:8787
    open http://127.0.0.1:8787/__ui

Point any webhook at http://127.0.0.1:8787/any/path you like. (Use a tunnel such as
cloudflared or ngrok if the sender needs a public URL.)
"""
import argparse
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_EVENTS = 200
MAX_BODY = 1_000_000

EVENTS = deque(maxlen=MAX_EVENTS)
LOCK = threading.Lock()
COUNTER = 0
CONFIG = {"status": 200, "response": '{"ok":true}'}

UI = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>hookcatch</title>
<style>
:root{--bg:#f6f4fc;--card:#fff;--fg:#1b1633;--mut:#6f6a8c;--line:#e5e0f5;--acc:#7c3aed;--soft:#f3eeff}
@media (prefers-color-scheme:dark){:root{--bg:#100c1f;--card:#1a1433;--fg:#ece8fb;--mut:#a59fc6;--line:#30284f;--acc:#a78bfa;--soft:#251c45}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 60px}
h1{margin:0;font-size:28px}.sub{color:var(--mut);margin:2px 0 14px}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:14px}
button{background:var(--acc);color:#fff;border:0;border-radius:9px;padding:7px 13px;font:600 13px inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
.ev{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:10px;overflow:hidden}
.head{display:flex;gap:10px;align-items:center;padding:10px 14px;cursor:pointer}
.m{font:700 12px ui-monospace,monospace;background:var(--acc);color:#fff;border-radius:6px;padding:2px 8px}
.p{font:14px ui-monospace,monospace;flex:1;word-break:break-all}.t{color:var(--mut);font-size:12px}
.body{display:none;padding:0 14px 14px}.ev.open .body{display:block}
h3{margin:12px 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}
pre{margin:0;background:var(--soft);border-radius:9px;padding:10px;white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,monospace}
.empty{color:var(--mut);text-align:center;padding:50px 0}
</style></head><body><main>
<h1>🪝 hookcatch</h1><p class="sub">Send any request to this server. It shows up here instantly.</p>
<div class="bar"><button id="clr">Clear</button><button id="pause" class="ghost">Pause</button><span class="t" id="count"></span></div>
<div id="list"></div></main>
<script>
const list=document.getElementById("list");let last=0,paused=false,events=[];
function curl(e){let c="curl -X "+e.method+" '"+location.origin+e.path+"'";
for(const[k,v]of Object.entries(e.headers)){if(["host","content-length","connection","accept-encoding"].includes(k.toLowerCase()))continue;c+=" -H '"+k+": "+v.replace(/'/g,"'\\''")+"'"}
if(e.body)c+=" --data-raw '"+e.body.replace(/'/g,"'\\''")+"'";return c}
function pretty(s){try{return JSON.stringify(JSON.parse(s),null,2)}catch(x){return s}}
function sec(t,txt){const h=document.createElement("h3");h.textContent=t;const p=document.createElement("pre");p.textContent=txt;return[h,p]}
function card(e){const d=document.createElement("div");d.className="ev";
const h=document.createElement("div");h.className="head";
const m=document.createElement("span");m.className="m";m.textContent=e.method;
const p=document.createElement("span");p.className="p";p.textContent=e.path;
const t=document.createElement("span");t.className="t";t.textContent=new Date(e.time*1000).toLocaleTimeString()+" · "+e.size+" B";
h.append(m,p,t);h.onclick=()=>d.classList.toggle("open");
const b=document.createElement("div");b.className="body";
b.append(...sec("Headers",Object.entries(e.headers).map(([k,v])=>k+": "+v).join("\n")));
if(Object.keys(e.query).length)b.append(...sec("Query",JSON.stringify(e.query,null,2)));
b.append(...sec("Body",e.body?pretty(e.body):"(empty)"));
const cb=document.createElement("button");cb.className="ghost";cb.style.marginTop="10px";cb.textContent="Copy as curl";
cb.onclick=async()=>{try{await navigator.clipboard.writeText(curl(e));cb.textContent="Copied ✓"}catch(x){cb.textContent="Copy failed"}setTimeout(()=>cb.textContent="Copy as curl",1300)};
b.append(cb);d.append(h,b);return d}
function draw(){list.textContent="";if(!events.length){const x=document.createElement("div");x.className="empty";x.textContent="Waiting for requests on "+location.host+" ...";list.append(x)}
for(const e of events)list.append(card(e));document.getElementById("count").textContent=events.length+" captured"}
async function poll(){if(!paused){try{const r=await fetch("/__events?after="+last);const j=await r.json();
if(j.length){for(const e of j){events.unshift(e);last=Math.max(last,e.id)}events=events.slice(0,200);draw()}}catch(x){}}setTimeout(poll,1000)}
document.getElementById("clr").onclick=async()=>{await fetch("/__clear",{method:"POST"});events=[];draw()};
document.getElementById("pause").onclick=e=>{paused=!paused;e.target.textContent=paused?"Resume":"Pause"};
draw();poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_any(self):
        global COUNTER
        url = urlparse(self.path)
        if url.path == "/__ui" and self.command == "GET":
            return self._send(200, UI, "text/html")
        if url.path == "/__events" and self.command == "GET":
            after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
            with LOCK:
                out = [e for e in EVENTS if e["id"] > after]
            return self._send(200, json.dumps(out))
        if url.path == "/__clear" and self.command == "POST":
            with LOCK:
                EVENTS.clear()
            return self._send(200, '{"ok":true}')

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, MAX_BODY)) if length else b""
        if length > MAX_BODY:
            self.rfile.read(length - MAX_BODY)
        body = raw.decode("utf-8", errors="replace")
        with LOCK:
            COUNTER += 1
            ev = {"id": COUNTER, "time": time.time(), "method": self.command, "path": self.path,
                  "query": {k: v if len(v) > 1 else v[0] for k, v in parse_qs(url.query).items()},
                  "headers": dict(self.headers.items()), "body": body, "size": length,
                  "client": self.client_address[0]}
            EVENTS.append(ev)
        print(f"[{time.strftime('%H:%M:%S')}] {self.command:<6} {self.path}  ({length} B)", flush=True)
        self._send(CONFIG["status"], CONFIG["response"])

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = handle_any


def main():
    ap = argparse.ArgumentParser(description="Catch and inspect webhooks locally.")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--status", type=int, default=200, help="HTTP status to answer with")
    ap.add_argument("--response", default='{"ok":true}', help="response body to answer with")
    a = ap.parse_args()
    CONFIG["status"], CONFIG["response"] = a.status, a.response
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"hookcatch listening on http://{a.host}:{a.port}\n  UI: http://{a.host}:{a.port}/__ui\n  Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
