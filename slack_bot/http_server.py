#!/usr/bin/env python3
import os, sys, json, threading
from pathlib import Path

_home = Path.home()
_studio = _home / "studio"
for _p in [_home / "persistent-packages", _studio]:
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from http.server import HTTPServer, BaseHTTPRequestHandler

RUNNER_SECRET = os.environ.get("RUNNER_SECRET_TOKEN", "")
PORT = int(os.environ.get("HTTP_SERVER_PORT", "8765"))

class JobHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "server": "course-qa-bot"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run-qa":
            self._json(404, {"error": "not found"})
            return
        token = self.headers.get("X-Runner-Token", "")
        if RUNNER_SECRET and token != RUNNER_SECRET:
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            params = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        if not params.get("course_id"):
            self._json(400, {"error": "course_id is required"})
            return
        job_id = params.get("job_id", "?")
        print(f"[server] Job accepted: {job_id} | course: {params['course_id']}", flush=True)
        self._json(202, {"accepted": True, "job_id": job_id})
        threading.Thread(target=_run_job, args=(params,), daemon=True).start()

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"[server] {fmt % args}", flush=True)

def _run_job(params):
    try:
        from slack_bot.runner import run
        result = run(params)
        print(f"[server] Job complete: {result.get('status')}", flush=True)
    except Exception as e:
        import traceback
        print(f"[server] Job failed: {e}", flush=True)
        traceback.print_exc()

def main():
    print(f"Course QA server on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), JobHandler).serve_forever()

if __name__ == "__main__":
    main()
