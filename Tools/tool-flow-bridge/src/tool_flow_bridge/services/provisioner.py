"""Per-tenant Node-RED runtime provisioner.

Node-RED is single-workspace, so tenant isolation = one Node-RED instance per tenant. This
module owns creating/tracking those instances and recording them in ``tenant_runtimes``.

Modes (``provisioner_mode``):
* ``static``     — one shared dev Node-RED wired via config (compose/local; the tested path).
* ``kubernetes`` — one Deployment + PVC + Service + egress-deny NetworkPolicy per tenant,
  rendered here and applied via the in-cluster API (production).
* ``docker``     — one host per tenant provisioned out-of-band (``nodered-<t8>``); the bridge
  just records the host (assisted/dev multi-tenant).

Secret model: the bridge is the SOLE trusted caller of every tenant Node-RED and the
NetworkPolicy isolates each instance, so the admin/invoke/credential secrets are
platform-wide (``static:*`` in static mode, ``env:*`` in k8s/docker mode) — per-tenant
secret rotation is a documented follow-up. This keeps secret resolution synchronous on the
hot invoke path (see ``services.secrets``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db import pool as db_pool
from ..db import queries

logger = structlog.get_logger(__name__)

# Sentinel tenant_id for the SINGLETON platform (public) Node-RED runtime row in
# flow_tools.tenant_runtimes (the nil UUID — never a real tenant). Kept in sync with migration
# 20260712_0006__platform_runtime.sql. See ``ensure_platform_runtime`` + ``PlatformProvisioner``.
PLATFORM_RUNTIME_TENANT_ID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class RuntimeSpec:
    internal_host: str
    http_node_root: str
    admin_token_ref: str
    invoke_secret_ref: str
    credential_secret_ref: str


class Provisioner(Protocol):
    mode: str

    async def provision(self, tenant_id: str, settings: Settings) -> RuntimeSpec: ...


class PlatformProvisioner(Protocol):
    """A provisioner for the SINGLETON platform (public) runtime — no tenant_id (it is not
    tenant-scoped) and egress-ALLOW (it may reach external providers)."""

    mode: str

    async def provision(self, settings: Settings) -> RuntimeSpec: ...


def _tenant_short(tenant_id: str) -> str:
    return tenant_id.replace("-", "")[:8].lower()


# ── static (shared dev instance) ─────────────────────────────────────────────────
class StaticProvisioner:
    mode = "static"

    async def provision(self, tenant_id: str, settings: Settings) -> RuntimeSpec:
        metrics.provision_total.labels("static", "ok").inc()
        return RuntimeSpec(
            internal_host=settings.static_nodered_internal_host,
            http_node_root=settings.static_nodered_http_node_root,
            admin_token_ref="static:admin",
            invoke_secret_ref="static:invoke",
            credential_secret_ref="static:credential",
        )


# ── docker / out-of-band (one host per tenant, provisioned externally) ───────────
class DockerTemplateProvisioner:
    mode = "docker"

    async def provision(self, tenant_id: str, settings: Settings) -> RuntimeSpec:
        t8 = _tenant_short(tenant_id)
        metrics.provision_total.labels("docker", "ok").inc()
        return RuntimeSpec(
            internal_host=f"http://nodered-{t8}:{settings.nodered_container_port}",
            http_node_root=settings.static_nodered_http_node_root,
            admin_token_ref="env:NODERED_ADMIN_TOKEN",
            invoke_secret_ref="env:NODERED_INVOKE_SECRET",
            credential_secret_ref="env:NODERED_CREDENTIAL_SECRET",
        )


# ── kubernetes (real applier) ────────────────────────────────────────────────────
class KubernetesProvisioner:
    mode = "kubernetes"

    async def provision(self, tenant_id: str, settings: Settings) -> RuntimeSpec:
        from .k8s import K8sClient, K8sError

        t8 = _tenant_short(tenant_id)
        name = f"nodered-{t8}"
        ns = settings.nodered_namespace
        port = settings.nodered_container_port
        objects = _render_objects(name, tenant_id, t8, settings)

        try:
            client = K8sClient()
        except K8sError as exc:
            metrics.provision_total.labels("kubernetes", "error").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"Provisioner unavailable: {exc}") from exc
        try:
            for obj in objects:
                await client.apply(obj, namespace=ns)
        except K8sError as exc:
            metrics.provision_total.labels("kubernetes", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, f"Failed to provision Node-RED: {exc}"
            ) from exc
        finally:
            await client.aclose()

        metrics.provision_total.labels("kubernetes", "ok").inc()
        host = f"http://{name}.{ns}.svc.cluster.local:{port}"
        return RuntimeSpec(
            internal_host=host,
            http_node_root=settings.static_nodered_http_node_root,
            admin_token_ref="env:NODERED_ADMIN_TOKEN",
            invoke_secret_ref="env:NODERED_INVOKE_SECRET",
            credential_secret_ref="env:NODERED_CREDENTIAL_SECRET",
        )


def _render_objects(name: str, tenant_id: str, t8: str, settings: Settings) -> list[dict[str, Any]]:
    ns = settings.nodered_namespace
    port = settings.nodered_container_port
    labels = {"app": name, "app.kubernetes.io/part-of": "cypherx-flow-tools", "cypherx.ai/tenant": t8}

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": settings.nodered_storage_size}},
        },
    }

    pod_spec: dict[str, Any] = {
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "fsGroup": 1000,
            # Restrict the syscall surface available to untrusted flow code.
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "node-red",
                "image": settings.nodered_image,
                "ports": [{"containerPort": port}],
                "env": [
                    {"name": "NODERED_ADMIN_ROOT", "value": settings.nodered_admin_root},
                    {"name": "NODERED_HTTP_NODE_ROOT", "value": settings.static_nodered_http_node_root},
                    {"name": "CYPHERX_TENANT_ID", "value": tenant_id},
                    {"name": "CYPHERX_INVOKE_SECRET_HEADER", "value": settings.nodered_invoke_secret_header},
                    # With a read-only rootfs, point HOME (npm cache/logs) at the writable emptyDir;
                    # flows/credentials still persist to the /data PVC (--userDir /data).
                    {"name": "HOME", "value": "/tmp"},
                ],
                "envFrom": [{"secretRef": {"name": "nodered-shared-secrets"}}],
                "resources": {
                    "limits": {"cpu": settings.nodered_cpu_limit, "memory": settings.nodered_memory_limit},
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": settings.nodered_read_only_root_fs,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "data", "mountPath": "/data"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
                "livenessProbe": {
                    "httpGet": {"path": settings.nodered_admin_root, "port": port},
                    "initialDelaySeconds": 20,
                },
            }
        ],
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": name}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if settings.nodered_runtime_class:
        pod_spec["runtimeClassName"] = settings.nodered_runtime_class

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]},
    }

    # Egress-deny NetworkPolicy: the workflow may only reach DNS + the explicit allow-list
    # CIDRs (NEVER internal platform services). Ingress only from the bridge.
    egress_rules: list[dict[str, Any]] = [
        {"to": [], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
    ]
    for cidr in settings.egress_allow_cidr_list:
        ip_block: dict[str, Any] = {"cidr": cidr}
        # A catch-all allow would otherwise re-expose the metadata endpoint + internal networks;
        # subtract them via ipBlock.except (valid only because they are all within 0.0.0.0/0).
        if cidr == "0.0.0.0/0" and settings.egress_block_cidr_list:
            ip_block["except"] = settings.egress_block_cidr_list
        egress_rules.append({"to": [{"ipBlock": ip_block}]})

    netpol = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "podSelector": {"matchLabels": {"app": name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "tool-flow-bridge"}}}],
                    "ports": [{"protocol": "TCP", "port": port}],
                }
            ],
            "egress": egress_rules,
        },
    }

    return [pvc, deployment, service, netpol]


# ── platform (public) runtime provisioners — singleton + egress-ALLOW ─────────────
# Mirror the per-tenant provisioners' 3 modes but platform-scoped: no tenant_id, one shared
# instance (``settings.platform_nodered_name``), and an egress-ALLOW NetworkPolicy so the runtime
# can reach the external search providers (the tenant runtimes are egress-DENY). The provider-key
# credential lives in the platform runtime's own Secret (charts/nodered-platform), not in the DB.


class StaticPlatformProvisioner:
    mode = "static"

    async def provision(self, settings: Settings) -> RuntimeSpec:
        metrics.provision_total.labels("platform-static", "ok").inc()
        return RuntimeSpec(
            internal_host=settings.static_platform_nodered_internal_host,
            http_node_root=settings.static_nodered_http_node_root,
            admin_token_ref="static:platform-admin",
            invoke_secret_ref="static:platform-invoke",
            credential_secret_ref="static:platform-credential",
        )


class DockerPlatformProvisioner:
    mode = "docker"

    async def provision(self, settings: Settings) -> RuntimeSpec:
        metrics.provision_total.labels("platform-docker", "ok").inc()
        return RuntimeSpec(
            internal_host=f"http://{settings.platform_nodered_name}:{settings.nodered_container_port}",
            http_node_root=settings.static_nodered_http_node_root,
            admin_token_ref="env:PLATFORM_NODERED_ADMIN_TOKEN",
            invoke_secret_ref="env:PLATFORM_NODERED_INVOKE_SECRET",
            credential_secret_ref="env:PLATFORM_NODERED_CREDENTIAL_SECRET",
        )


class KubernetesPlatformProvisioner:
    mode = "kubernetes"

    async def provision(self, settings: Settings) -> RuntimeSpec:
        from .k8s import K8sClient, K8sError

        name = settings.platform_nodered_name
        ns = settings.nodered_namespace
        port = settings.nodered_container_port
        objects = _render_platform_objects(settings)

        try:
            client = K8sClient()
        except K8sError as exc:
            metrics.provision_total.labels("platform-kubernetes", "error").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"Provisioner unavailable: {exc}") from exc
        try:
            for obj in objects:
                await client.apply(obj, namespace=ns)
        except K8sError as exc:
            metrics.provision_total.labels("platform-kubernetes", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, f"Failed to provision platform Node-RED: {exc}"
            ) from exc
        finally:
            await client.aclose()

        metrics.provision_total.labels("platform-kubernetes", "ok").inc()
        host = f"http://{name}.{ns}.svc.cluster.local:{port}"
        return RuntimeSpec(
            internal_host=host,
            http_node_root=settings.static_nodered_http_node_root,
            admin_token_ref="env:PLATFORM_NODERED_ADMIN_TOKEN",
            invoke_secret_ref="env:PLATFORM_NODERED_INVOKE_SECRET",
            credential_secret_ref="env:PLATFORM_NODERED_CREDENTIAL_SECRET",
        )


def _render_platform_objects(settings: Settings) -> list[dict[str, Any]]:
    """Render the singleton platform Node-RED objects (PVC + Deployment + Service + egress-ALLOW
    NetworkPolicy). Mirrors ``_render_objects`` (same hardened pod securityContext, RO rootfs, DNS
    egress) but platform-scoped and egress-PERMITTING: the NetworkPolicy additionally allows egress
    to ``platform_nodered_egress_allow_cidrs`` (default catch-all, with the metadata + RFC-1918
    block-list subtracted) so the runtime can reach the external search providers."""
    name = settings.platform_nodered_name
    ns = settings.nodered_namespace
    port = settings.nodered_container_port
    labels = {
        "app": name,
        "app.kubernetes.io/part-of": "cypherx-flow-tools",
        "cypherx.ai/role": "platform",
    }

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": settings.nodered_storage_size}},
        },
    }

    pod_spec: dict[str, Any] = {
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "node-red",
                "image": settings.nodered_image,
                "ports": [{"containerPort": port}],
                "env": [
                    {"name": "NODERED_ADMIN_ROOT", "value": settings.nodered_admin_root},
                    {"name": "NODERED_HTTP_NODE_ROOT", "value": settings.static_nodered_http_node_root},
                    {"name": "CYPHERX_RUNTIME_ROLE", "value": "platform"},
                    {"name": "CYPHERX_INVOKE_SECRET_HEADER", "value": settings.nodered_invoke_secret_header},
                    {"name": "HOME", "value": "/tmp"},
                ],
                # The platform provider-key credential is delivered via this Secret (envFrom).
                "envFrom": [{"secretRef": {"name": "nodered-platform-secrets"}}],
                "resources": {
                    "limits": {"cpu": settings.nodered_cpu_limit, "memory": settings.nodered_memory_limit},
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": settings.nodered_read_only_root_fs,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "data", "mountPath": "/data"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
                "livenessProbe": {
                    "httpGet": {"path": settings.nodered_admin_root, "port": port},
                    "initialDelaySeconds": 20,
                },
            }
        ],
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": name}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if settings.nodered_runtime_class:
        pod_spec["runtimeClassName"] = settings.nodered_runtime_class

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]},
    }

    # Egress-ALLOW NetworkPolicy: DNS + the platform allow-list. A catch-all 0.0.0.0/0 still
    # subtracts the metadata endpoint + internal/RFC-1918 ranges (ipBlock.except) so the platform
    # runtime can reach EXTERNAL providers but NEVER internal platform services. Ingress: bridge only.
    egress_rules: list[dict[str, Any]] = [
        {"to": [], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
    ]
    for cidr in settings.platform_egress_allow_cidr_list:
        ip_block: dict[str, Any] = {"cidr": cidr}
        if cidr == "0.0.0.0/0" and settings.egress_block_cidr_list:
            ip_block["except"] = settings.egress_block_cidr_list
        egress_rules.append({"to": [{"ipBlock": ip_block}]})

    netpol = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "podSelector": {"matchLabels": {"app": name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "tool-flow-bridge"}}}],
                    "ports": [{"protocol": "TCP", "port": port}],
                }
            ],
            "egress": egress_rules,
        },
    }

    return [pvc, deployment, service, netpol]


def get_provisioner(settings: Settings) -> Provisioner:
    mode = settings.provisioner_mode.lower()
    if mode == "kubernetes":
        return KubernetesProvisioner()
    if mode == "docker":
        return DockerTemplateProvisioner()
    return StaticProvisioner()


def get_platform_provisioner(settings: Settings) -> PlatformProvisioner:
    mode = settings.provisioner_mode.lower()
    if mode == "kubernetes":
        return KubernetesPlatformProvisioner()
    if mode == "docker":
        return DockerPlatformProvisioner()
    return StaticPlatformProvisioner()


async def ensure_runtime(
    pool: AsyncConnectionPool,
    tenant_id: str,
    provisioner: Provisioner,
    settings: Settings,
) -> dict[str, Any]:
    """Return the tenant's Node-RED runtime row, provisioning + recording it if absent."""

    async def _get(conn):
        return await queries.get_tenant_runtime(conn, tenant_id)

    existing = await db_pool.in_tenant(pool, tenant_id, _get)
    if existing is not None and existing["status"] in ("running", "provisioning"):
        return existing

    spec = await provisioner.provision(tenant_id, settings)

    async def _upsert(conn):
        return await queries.upsert_tenant_runtime(
            conn,
            tenant_id,
            internal_host=spec.internal_host,
            http_node_root=spec.http_node_root,
            admin_token_ref=spec.admin_token_ref,
            invoke_secret_ref=spec.invoke_secret_ref,
            credential_secret_ref=spec.credential_secret_ref,
            status="running",
        )

    row = await db_pool.in_tenant(pool, tenant_id, _upsert)
    logger.info("tenant_runtime_ready", tenant_id=tenant_id, mode=provisioner.mode)
    return row


async def ensure_platform_runtime(
    pool: AsyncConnectionPool,
    provisioner: PlatformProvisioner,
    settings: Settings,
) -> dict[str, Any]:
    """Return the SINGLETON platform (public) Node-RED runtime row, provisioning + recording it if
    absent. Mirrors ``ensure_runtime`` but platform-scoped: it reads/writes the well-known SENTINEL
    row (``PLATFORM_RUNTIME_TENANT_ID``) in PLATFORM context (``in_platform`` — empty GUC), which the
    ``p_tenant_runtimes_platform_*`` policies (migration 0006) admit."""

    async def _get(conn):
        return await queries.get_tenant_runtime(conn, PLATFORM_RUNTIME_TENANT_ID)

    existing = await db_pool.in_platform(pool, _get)
    if existing is not None and existing["status"] in ("running", "provisioning"):
        return existing

    spec = await provisioner.provision(settings)

    async def _upsert(conn):
        return await queries.upsert_tenant_runtime(
            conn,
            PLATFORM_RUNTIME_TENANT_ID,
            internal_host=spec.internal_host,
            http_node_root=spec.http_node_root,
            admin_token_ref=spec.admin_token_ref,
            invoke_secret_ref=spec.invoke_secret_ref,
            credential_secret_ref=spec.credential_secret_ref,
            status="running",
        )

    row = await db_pool.in_platform(pool, _upsert)
    logger.info("platform_runtime_ready", mode=provisioner.mode)
    return row
