"""Serve the UI in an ordinary browser, wired to the real back end.

For development and automated UI testing: the page gets a stand-in for
`window.pywebview.api` that forwards each call over HTTP to the same `Api`
object the desktop app uses. The native folder dialog is the one thing that
cannot work here; call `addFolderFlow("/some/path")` from the console.

    uv run python scripts/dev_server.py [port]
"""

from __future__ import annotations

import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from folder_search.app import UI_DIR, Api

SHIM = b"""<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
  const r = await fetch('/api/' + name, { method: 'POST', body: JSON.stringify(args) });
  return r.json();
}})};
window.addEventListener('DOMContentLoaded', () => window.dispatchEvent(new Event('pywebviewready')));
</script>"""

api = Api()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI_DIR), **kw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (UI_DIR / "index.html").read_bytes()
            html = html.replace(b'<script src="app.js">', SHIM + b'<script src="app.js">')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
            return
        super().do_GET()

    def do_POST(self):
        name = self.path.removeprefix("/api/")
        args = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"[]")
        if name.startswith("_") or not hasattr(api, name):
            self.send_error(404)
            return
        try:
            result = getattr(api, name)(*args)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
