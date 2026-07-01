"""Scope-ownership + cross-principal visibility rules (THE leak-regression guard).

Memory ownership is **principal-only by default**: a memory belongs to the
(principal_type, principal_id) that stored it. Visibility to a DIFFERENT principal in
the same tenant is governed by two things:

1. the memory's own ``scope``:
   * ``principal_only`` — NEVER visible to another principal. Full stop. This is the
     default and is what makes the cross-end-user leak impossible.
   * ``tenant_shared``  — eligible to be visible tenant-wide, BUT only if the tenant's
     ``user_scope_visibility`` policy permits it.
2. the tenant's ``user_scope_visibility`` policy:
   * ``isolated`` (DEFAULT) — even ``tenant_shared`` memories are visible ONLY to their
     owner. The strongest posture; nothing crosses principals.
   * ``tenant``            — ``tenant_shared`` memories are visible to any principal in
     the tenant. ``principal_only`` memories STILL never cross.

A caller can ALWAYS see their own memories regardless of scope/policy. The functions
here are pure (no DB) so they are the single source of truth shared by the Postgres repo
(which encodes the same predicate in SQL) and the in-memory repo, and are unit-tested
directly as the regression guard.
"""

from __future__ import annotations

from typing import Literal

UserScopeVisibility = Literal["isolated", "tenant"]


def can_view(
    *,
    caller_type: str,
    caller_id: str,
    owner_type: str,
    owner_id: str,
    memory_scope: str,
    user_scope_visibility: str,
) -> bool:
    """Return True iff the caller principal may VIEW a memory owned by (owner_type, owner_id).

    THE invariant: a ``principal_only`` memory owned by a different principal is NEVER
    viewable, regardless of the tenant policy. A ``tenant_shared`` memory crosses only
    when ``user_scope_visibility == 'tenant'``.
    """
    # The owner can always see their own memories.
    if caller_type == owner_type and caller_id == owner_id:
        return True
    # A different principal: principal_only never crosses.
    if memory_scope != "tenant_shared":
        return False
    # tenant_shared crosses only under the 'tenant' policy.
    return user_scope_visibility == "tenant"
