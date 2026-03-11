#!/usr/bin/env python3
"""Deploy webhook receiver — runs as deploy-agent container."""

import hashlib, hmac, json, logging, os, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ.get("DEPLOY_WEBHOOK_SECRET", "").encode()
SCRIPT = "/deploy/deploy.sh"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        token = self.headers.get("X-Deploy-Secret", "")
        if not SECRET or not hmac.compare_digest(token.encode(), SECRET):
            log.warning("Rejected deploy from %s", self.address_string())
            self._respond(403, {"error": "forbidden"})
            return
        payload = json.loads(body) if body else {}
        image = payload.get("image", "ghcr.io/cocacolasante/sentinel:latest")
        sha = payload.get("sha", "?")
        log.info("Deploy: image=%s sha=%s", image, sha)
        subprocess.Popen([SCRIPT, image, sha], stdout=open("/tmp/deploy.log", "a"), stderr=subprocess.STDOUT)
        self._respond(202, {"status": "deploy started", "sha": sha})

    def _respond(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *a):
        log.info("%s %s", self.address_string(), fmt % a)


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 9000))
    log.info("Webhook listening on :%d", port)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
