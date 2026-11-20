"""Run beside app.py, from the CRM directory. Tunnels should target port 8001."""
import hmac
import json
import os
from wsgiref.simple_server import make_server

from app import update


def endpoint(tool_key, bearer):
    def handle(env, respond):
        status = "403 Forbidden"
        result = {"error": "Forbidden"}
        if env["REQUEST_METHOD"] == "POST" and env["PATH_INFO"] == "/tool":
            try:
                size = int(env.get("CONTENT_LENGTH") or 0)
                if not 0 < size <= 32768:
                    raise ValueError("Invalid body size")
                body = json.loads(env["wsgi.input"].read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                key = body.pop("integration_key", None)
                body_ok = isinstance(key, str) and hmac.compare_digest(key.encode(), tool_key.encode())
                header = env.get("HTTP_AUTHORIZATION", "")
                header_ok = len(bearer) >= 32 and hmac.compare_digest(header.encode(), ("Bearer " + bearer).encode())
                if body_ok or header_ok:
                    session_id = env.get("HTTP_X_SESSION_ID", "").strip()
                    if session_id:
                        body["call_id"] = session_id
                    result = update(body)
                    status = "200 OK"
            except (ValueError, TypeError, KeyError, RecursionError):
                status = "400 Bad Request"
                result = {"error": "Check call_id, action, and intake fields"}
        respond(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
        return [json.dumps(result).encode()]
    return handle


if __name__ == "__main__":
    key = os.environ.get("MR_EXOTEL_KEY", "")
    if len(key) < 32:
        raise SystemExit("Set MR_EXOTEL_KEY to a random secret of at least 32 characters")
    with make_server("127.0.0.1", 8001, endpoint(key, os.getenv("MR_TOOL_SECRET", ""))) as server:
        print("MR CARE Exotel intake listening on 127.0.0.1:8001", flush=True)
        server.serve_forever()
