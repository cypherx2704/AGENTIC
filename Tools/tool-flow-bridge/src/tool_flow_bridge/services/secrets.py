"""Secret-reference resolver.

The DB stores only secret *references* (Contract-14 convention — never secret material).
This resolver turns a ref into the actual value at use time:

* ``static:admin`` / ``static:invoke`` / ``static:credential`` — the single shared dev
  Node-RED secrets from config (``provisioner_mode=static``, compose/local).
* ``env:NAME`` — read ``NAME`` from the process environment (k8s injects per-tenant
  secrets as env vars via the Deployment's ``envFrom``/``secretKeyRef``).
* anything else — treated as a literal (test seams / explicit values).
"""

from __future__ import annotations

import os

from ..core.config import Settings


def resolve_secret(ref: str, settings: Settings) -> str:
    if ref == "static:admin":
        return settings.static_nodered_admin_token
    if ref == "static:invoke":
        return settings.static_nodered_invoke_secret
    if ref == "static:credential":
        return settings.static_nodered_credential_secret
    if ref.startswith("env:"):
        return os.environ.get(ref[4:], "")
    return ref
