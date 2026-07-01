"""Discovery resolution logic (pure) — tenant-priority shadowing + invoke URL.

The DB returns the UNION of platform skills (tenant_id IS NULL) and the caller's own
skills (RLS-scoped). This module folds that list into the discovery view:

* **Tenant priority (shadowing):** when a tenant has a skill with the SAME name as a
  platform skill, the tenant's skill WINS — the platform skill is hidden from that
  tenant's discovery. A tenant cannot see another tenant's skill at all (RLS), so the
  only collision possible is tenant-vs-platform.
* **Invoke URL:** resolved from the skill's manifest (``base_url`` when present, else a
  conventional ``http://<name>:8080``) — this is the URL xAgent's skill loop posts to.

Keeping this pure (no DB/HTTP) makes the UNION + shadowing + version-pin behaviour
unit-testable with plain dicts.
"""

from __future__ import annotations

from typing import Any

# Default in-cluster invoke port for a skill server when the manifest omits base_url.
_DEFAULT_SKILL_PORT = 8080


def shadow_by_tenant_priority(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse (name -> skill) keeping the tenant row over a platform row of the same name.

    ``rows`` are skill rows each carrying ``name`` and ``is_platform`` (bool). For each
    distinct name, a non-platform (tenant) row shadows a platform row. Order of the
    returned list is by name (stable).
    """
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["name"]
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = row
            continue
        # A tenant row (is_platform False) wins over a platform row (is_platform True).
        if existing.get("is_platform") and not row.get("is_platform"):
            by_name[name] = row
    return [by_name[name] for name in sorted(by_name)]


def resolve_invoke_url(manifest: dict[str, Any] | None, name: str) -> str:
    """Resolve the skill's invoke base URL from its manifest, else a conventional name."""
    if isinstance(manifest, dict):
        base = manifest.get("base_url")
        if isinstance(base, str) and base.strip():
            return base.rstrip("/")
    return f"http://{name}:{_DEFAULT_SKILL_PORT}"


def build_skill_view(
    skill_row: dict[str, Any],
    *,
    manifest: dict[str, Any] | None,
    resolved_version: str | None,
    capabilities: list[dict[str, Any]],
    health: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the discovery response object for one resolved skill.

    Returns the skill's identity, the resolved manifest + version, its invoke URL, the
    required scopes (manifest-declared, falling back to the capability rows), and its
    current health status.
    """
    name = skill_row["name"]
    required_scopes: list[str] = []
    if isinstance(manifest, dict) and isinstance(manifest.get("required_scopes"), list):
        required_scopes = [str(s) for s in manifest["required_scopes"]]
    elif capabilities:
        # Fall back to the per-capability required scopes (deduped, stable order).
        seen: dict[str, None] = {}
        for cap in capabilities:
            scope = cap.get("required_scope")
            if scope:
                seen.setdefault(str(scope), None)
        required_scopes = list(seen)

    return {
        "skill_id": skill_row.get("skill_id"),
        "name": name,
        "owner": "platform" if skill_row.get("is_platform") else "tenant",
        "status": skill_row.get("status"),
        "version": resolved_version,
        "invoke_url": resolve_invoke_url(manifest, name),
        "required_scopes": required_scopes,
        "capabilities": [c.get("capability") for c in capabilities],
        "health": (health or {}).get("status", "unknown"),
        "manifest": manifest,
    }
