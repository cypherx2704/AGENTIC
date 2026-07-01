#!/usr/bin/env python3
"""CypherX prototype demo — Backend-for-Frontend + single-page agent runner.

Zero third-party dependencies (Python 3.10+ stdlib only). Run it with the system
Python; no venv / pip install required:

    python frontend/demo/server.py
    # then open http://localhost:8090

It serves the single-page UI (index.html) and proxies to the already-running
CypherX services, hiding the 7-step credential chain behind one endpoint:

  GET  /                 -> the agent-runner UI
  GET  /api/health       -> readyz of auth/llms/guardrails/xagent
  GET  /api/agent        -> the demo agent's identity + config
  POST /api/run {message}-> mint worker JWT -> POST /v1/tasks -> return the
                            Contract-3 task response + per-step timeline

The whole point is the *timeline*: every task returns its ordered audit steps
(guardrail_check_input -> llm_call -> guardrail_check_output) with status,
duration and tokens, plus total tokens_used + cost_usd + trace_id — surfacing the
data the runtime already persists so auth + guardrails are visible, not asserted.

Provisioning (bootstrap super-admin -> create agent -> issue key -> register
runtime) runs automatically on first use and is cached in demo_credentials.json.
Because Auth bootstrap is one-time (410 Gone after first success), provisioning
first clears the auth.bootstrap_state sentinel via `docker exec` (best effort).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / "demo_credentials.json"
INDEX = HERE / "index.html"

# ── Endpoints / config (override via env) ────────────────────────────────────────
AUTH = os.getenv("AUTH_URL", "http://localhost:8080")
XAGENT = os.getenv("XAGENT_URL", "http://localhost:8083")
SERVICES = {
    "auth": AUTH,
    "llms": os.getenv("LLMS_URL", "http://localhost:8085"),
    "guardrails": os.getenv("GUARDRAILS_URL", "http://localhost:8086"),
    "xagent": XAGENT,
}
BOOT_TOKEN = os.getenv("BOOTSTRAP_TOKEN", "local-bootstrap-token-change-me")
PG_CONTAINER = os.getenv("PG_CONTAINER", "cypherx-postgres")
PG_USER = os.getenv("PG_USER", "cypherx_admin")
PG_DB = os.getenv("PG_DB", "cypherx_platform")
PORT = int(os.getenv("PORT", "8090"))
SCOPES = ["agent:execute", "llm:invoke", "guardrails:check"]
SYSTEM_PROMPT = os.getenv("DEMO_SYSTEM_PROMPT", "You are a helpful assistant. Answer concisely.")
MODEL = os.getenv("DEMO_MODEL", "smart")

_state = {"tenant_id": None, "agent_id": None, "worker_api_key": None, "error": None}
_lock = threading.Lock()


# ── tiny HTTP client (stdlib) ────────────────────────────────────────────────────
def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def _http(method: str, url: str, headers: dict | None = None, body=None, timeout: float = 25.0):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _maybe_json(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


# ── provisioning (cached) ────────────────────────────────────────────────────────
def _reset_bootstrap_sentinel() -> None:
    """Best-effort: clear the one-time bootstrap sentinel so we can bootstrap fresh.

    Prefer a direct psql against a real DSN when one is provided (DEMO_DB_URL or
    DATABASE_URL) — this is the Neon-compatible path (sslmode lives in the URL) used
    when there is no local Postgres container. Falls back to the legacy `docker exec`
    against a local PG container otherwise. Best-effort: if psql/docker is absent or
    the DB is unreachable, bootstrap may 410 and we surface that.
    """
    db_url = os.getenv("DEMO_DB_URL") or os.getenv("DATABASE_URL")
    try:
        if db_url:
            subprocess.run(
                ["psql", db_url, "-c", "DELETE FROM auth.bootstrap_state;"],
                capture_output=True, timeout=20,
            )
        else:
            subprocess.run(
                ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB,
                 "-c", "DELETE FROM auth.bootstrap_state;"],
                capture_output=True, timeout=20,
            )
    except Exception:  # noqa: BLE001 — if docker/psql absent, bootstrap may 410 and we surface it
        pass


def _mint_worker_jwt(tenant: str, agent_id: str, api_key: str) -> str | None:
    st, b = _http("POST", f"{AUTH}/v1/agents/{agent_id}/token",
                  {"X-Tenant-ID": tenant}, {"api_key": api_key, "scopes": SCOPES})
    if st == 200 and isinstance(b, dict) and b.get("token"):
        return b["token"]
    return None


def _provision(force: bool = False) -> bool:
    """Ensure a demo worker agent exists; cache its credentials. Returns success."""
    with _lock:
        if not force and CRED_FILE.exists():
            try:
                c = json.loads(CRED_FILE.read_text())
                if _mint_worker_jwt(c["tenant_id"], c["agent_id"], c["worker_api_key"]):
                    _state.update(c)
                    _state["error"] = None
                    return True
            except Exception:  # noqa: BLE001 — stale/invalid cache -> reprovision
                pass

        if os.getenv("DEMO_RESET_BOOTSTRAP", "1") == "1":
            _reset_bootstrap_sentinel()
        st, b = _http("POST", f"{AUTH}/v1/admin/bootstrap",
                      {"X-Bootstrap-Token": BOOT_TOKEN}, {"name": "demo-admin-" + secrets.token_hex(3)})
        if st != 201:
            hint = " — bootstrap is one-time; clear auth.bootstrap_state (DEMO_RESET_BOOTSTRAP=1)" if st == 410 else ""
            _state["error"] = f"bootstrap failed ({st}){hint}: {b}"
            return False
        tenant, admin_aid, admin_key = b["tenant_id"], b["agent_id"], b["api_key"]

        st, b = _http("POST", f"{AUTH}/v1/agents/{admin_aid}/token",
                      {"X-Tenant-ID": tenant}, {"api_key": admin_key, "scopes": ["platform:admin"]})
        if st != 200:
            _state["error"] = f"admin token failed ({st}): {b}"
            return False
        admin_jwt = b["token"]

        wname = "demo-agent-" + secrets.token_hex(3)
        st, b = _http("POST", f"{AUTH}/v1/agents",
                      {"Authorization": f"Bearer {admin_jwt}", "X-Tenant-ID": tenant},
                      {"name": wname, "allowed_scopes": SCOPES})
        if st != 201:
            _state["error"] = f"create agent failed ({st}): {b}"
            return False
        agent_id = b["agent_id"]

        st, b = _http("POST", f"{AUTH}/v1/agents/{agent_id}/keys",
                      {"Authorization": f"Bearer {admin_jwt}", "X-Tenant-ID": tenant},
                      {"scopes": SCOPES, "name": "demo-key"})
        if st != 201:
            _state["error"] = f"issue key failed ({st}): {b}"
            return False
        worker_key = b["api_key"]

        st, b = _http("POST", f"{XAGENT}/v1/agents/{agent_id}/runtime",
                      {"Authorization": f"Bearer {admin_jwt}"},
                      {"name": wname, "system_prompt": SYSTEM_PROMPT, "llm_model": MODEL,
                       "max_tokens": 256, "temperature": 0.0})
        if st != 200:
            _state["error"] = f"register runtime failed ({st}): {b}"
            return False

        creds = {"tenant_id": tenant, "agent_id": agent_id, "worker_api_key": worker_key,
                 "system_prompt": SYSTEM_PROMPT, "model": MODEL}
        try:
            CRED_FILE.write_text(json.dumps(creds, indent=2))
        except Exception:  # noqa: BLE001
            pass
        _state.update(creds)
        _state["error"] = None
        return True


# ── task run (unified shape for the UI) ──────────────────────────────────────────
def _normalize(task: dict | None) -> dict:
    task = task or {}
    steps = [
        {"step": s.get("step"), "status": s.get("status"),
         "duration_ms": s.get("duration_ms"), "tokens": s.get("tokens")}
        for s in (task.get("task_steps") or [])
    ]
    # Tolerant answer extraction: the Contract-3 happy body is {output:{message}}, but fall back
    # to text/content/plain-string/dumps so the agent's reply never silently vanishes if the
    # shape shifts (the worst failure mode in a live demo: green badge, no answer).
    out = task.get("output")
    if isinstance(out, dict):
        msg = out.get("message") or out.get("text") or out.get("content")
        if msg is None and out:
            msg = json.dumps(out)
    elif isinstance(out, str):
        msg = out
    else:
        msg = None
    return {
        "status": task.get("status"),
        "output": msg,
        "steps": steps,
        "tokens_used": task.get("tokens_used", 0),
        "cost_usd": task.get("cost_usd", 0.0),
        "duration_ms": task.get("duration_ms"),
        "trace_id": task.get("trace_id"),
        "task_id": task.get("task_id"),
    }


def _creds() -> dict:
    with _lock:
        return {"tenant_id": _state.get("tenant_id"), "agent_id": _state.get("agent_id"),
                "worker_api_key": _state.get("worker_api_key")}


def run_task(message: str) -> dict:
    if not _state.get("agent_id") and not _provision():
        return {"ok": False, "error": _state.get("error") or "provisioning failed"}

    c = _creds()
    jwt = _mint_worker_jwt(c["tenant_id"], c["agent_id"], c["worker_api_key"])
    if not jwt:  # cache may be stale (DB reset) — reprovision once
        if not _provision(force=True):
            return {"ok": False, "error": _state.get("error") or "provisioning failed"}
        c = _creds()
        jwt = _mint_worker_jwt(c["tenant_id"], c["agent_id"], c["worker_api_key"])
        if not jwt:
            return {"ok": False, "error": "could not mint worker token"}

    st, b = _http("POST", f"{XAGENT}/v1/tasks", {"Authorization": f"Bearer {jwt}"},
                  {"agent_id": c["agent_id"], "input": {"message": message}})

    if st == 200:
        res = _normalize(b)
        res.update({"ok": True, "blocked": False, "http_status": 200, "guardrail_message": None})
        return res

    if st == 422:  # guardrail block on input — fetch the (failed) timeline to show the step
        err = (b or {}).get("error", {}) if isinstance(b, dict) else {}
        task_id = ((err.get("details") or {}).get("task_id")
                   or (b.get("task_id") if isinstance(b, dict) else None)
                   or err.get("task_id"))
        timeline = None
        if task_id:
            gst, gb = _http("GET", f"{XAGENT}/v1/tasks/{task_id}", {"Authorization": f"Bearer {jwt}"})
            timeline = gb if gst == 200 else None
        res = _normalize(timeline)
        # The blocked input-check is the headline of this demo case — always show it even if the
        # timeline GET was unavailable.
        if not res["steps"]:
            res["steps"] = [{"step": "guardrail_check_input", "status": "failed",
                             "duration_ms": None, "tokens": None}]
        if res["status"] is None:
            res["status"] = "failed"
        res.update({"ok": True, "blocked": True, "http_status": 422,
                    "guardrail_message": err.get("message"), "trace_id": res.get("trace_id") or err.get("trace_id")})
        return res

    return {"ok": False, "error": f"task submission failed (HTTP {st})", "raw": b, "http_status": st}


def health() -> dict:
    out = {}
    for name, base in SERVICES.items():
        st, _ = _http("GET", f"{base}/readyz", timeout=2.5)
        out[name] = st
    return {"services": out, "provisioned": bool(_state.get("agent_id")),
            "agent_id": _state.get("agent_id"), "tenant_id": _state.get("tenant_id"),
            "error": _state.get("error")}


# ── HTTP server ──────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def log_message(self, *_args) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        try:
            if self.path in ("/", "/index.html"):
                if INDEX.exists():
                    self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(500, b"index.html missing", "text/plain")
            elif self.path == "/api/health":
                self._json(200, health())
            elif self.path == "/api/agent":
                self._json(200, {"tenant_id": _state.get("tenant_id"), "agent_id": _state.get("agent_id"),
                                 "system_prompt": _state.get("system_prompt") or SYSTEM_PROMPT,
                                 "model": _state.get("model") or MODEL})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 — never drop the socket; always return JSON
            try:
                self._json(500, {"ok": False, "error": str(e)})
            except Exception:  # noqa: BLE001 — client already gone
                pass

    def do_POST(self) -> None:
        try:
            if self.path != "/api/run":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:  # noqa: BLE001
                self._json(400, {"ok": False, "error": "invalid JSON body"})
                return
            m = payload.get("message")
            message = m.strip() if isinstance(m, str) else ""
            if not message:
                self._json(400, {"ok": False, "error": "message is required"})
                return
            self._json(200, run_task(message))
        except Exception as e:  # noqa: BLE001
            try:
                self._json(500, {"ok": False, "error": str(e)})
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    print(f"CypherX demo BFF on http://localhost:{PORT}  (auth={AUTH} xagent={XAGENT})")
    try:
        if _provision():
            print(f"  provisioned demo agent {_state['agent_id']} (tenant {_state['tenant_id']})")
        else:
            print(f"  WARN provisioning deferred: {_state.get('error')} (will retry on first run)")
    except Exception as e:  # noqa: BLE001
        print(f"  WARN provisioning error: {e}")
    # Bind loopback by default (the BFF is unauthenticated and discloses agent config); set
    # BIND=0.0.0.0 only on a trusted/isolated network if a remote machine must reach the demo.
    bind = os.getenv("BIND", "127.0.0.1")
    ThreadingHTTPServer((bind, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
