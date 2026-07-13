"""Minimal in-cluster Kubernetes REST client (dependency-free, httpx).

Used by the KubernetesProvisioner to create-or-replace the per-tenant Node-RED objects
(Deployment, PVC, Service, NetworkPolicy) from the pod's mounted ServiceAccount. No
``kubernetes`` package dependency — just the in-cluster API over httpx. Not used in
``static`` provisioner mode (compose/local), so its absence never affects the dev path.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class K8sError(Exception):
    pass


class K8sClient:
    """Create-or-replace applier against the in-cluster API server."""

    def __init__(self) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise K8sError("Not running in-cluster (KUBERNETES_SERVICE_HOST unset).")
        self._base = f"https://{host}:{port}"
        try:
            with open(f"{_SA_DIR}/token", encoding="utf-8") as fh:
                self._token = fh.read().strip()
        except OSError as exc:
            raise K8sError(f"Cannot read ServiceAccount token: {exc}") from exc
        ca = f"{_SA_DIR}/ca.crt"
        self._verify: str | bool = ca if os.path.exists(ca) else False
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._token}"},
            verify=self._verify,
            timeout=15.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _collection_path(api_version: str, kind: str, namespace: str) -> str:
        # Namespaced collection path for the kind (lowercased + pluralized simply).
        plural = _KIND_PLURALS[kind]
        prefix = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
        return f"{prefix}/namespaces/{namespace}/{plural}"

    async def apply(self, obj: dict[str, Any], *, namespace: str) -> None:
        """Create the object, or replace it if it already exists."""
        api_version = obj["apiVersion"]
        kind = obj["kind"]
        name = obj["metadata"]["name"]
        coll = self._collection_path(api_version, kind, namespace)

        resp = await self._client.post(coll, json=obj)
        if resp.status_code in (200, 201):
            return
        if resp.status_code == 409:
            # Exists — fetch resourceVersion and PUT a replace.
            got = await self._client.get(f"{coll}/{name}")
            if got.status_code == 200:
                current = got.json()
                obj.setdefault("metadata", {})["resourceVersion"] = current["metadata"][
                    "resourceVersion"
                ]
                put = await self._client.put(f"{coll}/{name}", json=obj)
                if put.status_code in (200, 201):
                    return
                raise K8sError(f"Replace {kind}/{name} failed ({put.status_code}): {put.text}")
        raise K8sError(f"Apply {kind}/{name} failed ({resp.status_code}): {resp.text}")

    async def exists(self, api_version: str, kind: str, name: str, *, namespace: str) -> bool:
        coll = self._collection_path(api_version, kind, namespace)
        resp = await self._client.get(f"{coll}/{name}")
        return resp.status_code == 200


_KIND_PLURALS = {
    "Deployment": "deployments",
    "Service": "services",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "NetworkPolicy": "networkpolicies",
    "Secret": "secrets",
    "ConfigMap": "configmaps",
}
