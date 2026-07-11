"""Provisioner tests — pure selection + rendering (no live Kubernetes, no infra).

Covers:
* ``get_provisioner`` maps ``provisioner_mode`` to the right implementation.
* ``StaticProvisioner.provision`` returns the shared static host + ``static:*`` refs
  for ANY tenant (async; asyncio_mode=auto handles the await).
* ``_render_objects`` emits the four per-tenant objects (PVC, Deployment, Service,
  NetworkPolicy) with a hardened securityContext and an egress-deny NetworkPolicy
  whose default egress allows ONLY DNS and whose ingress is restricted to the bridge.

Nothing here touches the API server, Valkey, or Postgres: ``_render_objects`` is a pure
function and ``StaticProvisioner.provision`` only bumps a Prometheus counter.
"""

from __future__ import annotations

from tool_flow_bridge.core.config import Settings
from tool_flow_bridge.services.provisioner import (
    KubernetesProvisioner,
    StaticProvisioner,
    _render_objects,
    _tenant_short,
    get_provisioner,
)

TENANT_A = "11111111-2222-3333-4444-555555555555"
TENANT_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _by_kind(objects, kind):
    matches = [o for o in objects if o["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


# ── get_provisioner selection ────────────────────────────────────────────────────
def test_get_provisioner_static():
    prov = get_provisioner(Settings(provisioner_mode="static"))
    assert isinstance(prov, StaticProvisioner)
    assert prov.mode == "static"


def test_get_provisioner_kubernetes():
    prov = get_provisioner(Settings(provisioner_mode="kubernetes"))
    assert isinstance(prov, KubernetesProvisioner)
    assert prov.mode == "kubernetes"


def test_get_provisioner_kubernetes_case_insensitive():
    # get_provisioner lowercases the mode before matching.
    prov = get_provisioner(Settings(provisioner_mode="Kubernetes"))
    assert isinstance(prov, KubernetesProvisioner)


def test_get_provisioner_unknown_defaults_to_static():
    prov = get_provisioner(Settings(provisioner_mode="bogus"))
    assert isinstance(prov, StaticProvisioner)


# ── StaticProvisioner.provision ──────────────────────────────────────────────────
async def test_static_provision_returns_shared_host_and_static_refs():
    settings = Settings(provisioner_mode="static")
    spec = await StaticProvisioner().provision(TENANT_A, settings)

    assert spec.internal_host == settings.static_nodered_internal_host
    assert spec.http_node_root == settings.static_nodered_http_node_root
    assert spec.admin_token_ref == "static:admin"
    assert spec.invoke_secret_ref == "static:invoke"
    assert spec.credential_secret_ref == "static:credential"


async def test_static_provision_is_tenant_agnostic():
    settings = Settings(provisioner_mode="static")
    prov = StaticProvisioner()
    spec_a = await prov.provision(TENANT_A, settings)
    spec_b = await prov.provision(TENANT_B, settings)
    # Same shared instance + same platform-wide refs regardless of tenant.
    assert spec_a == spec_b


# ── _render_objects: object set ──────────────────────────────────────────────────
def _render(settings: Settings | None = None):
    settings = settings or Settings(provisioner_mode="kubernetes")
    t8 = _tenant_short(TENANT_A)
    name = f"nodered-{t8}"
    return _render_objects(name, TENANT_A, t8, settings), name, settings


def test_render_objects_emits_all_four_kinds():
    objects, _, _ = _render()
    kinds = {o["kind"] for o in objects}
    assert kinds == {"PersistentVolumeClaim", "Deployment", "Service", "NetworkPolicy"}


def test_render_pvc_shape():
    objects, name, settings = _render()
    pvc = _by_kind(objects, "PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == name
    assert pvc["metadata"]["namespace"] == settings.nodered_namespace
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert (
        pvc["spec"]["resources"]["requests"]["storage"] == settings.nodered_storage_size
    )


def test_render_service_shape():
    objects, name, settings = _render()
    svc = _by_kind(objects, "Service")
    assert svc["spec"]["selector"] == {"app": name}
    assert svc["spec"]["ports"] == [
        {"port": settings.nodered_container_port, "targetPort": settings.nodered_container_port}
    ]


# ── Deployment securityContext hardening ─────────────────────────────────────────
def test_deployment_pod_security_context_runs_as_nonroot():
    objects, _, _ = _render()
    deployment = _by_kind(objects, "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True


def test_deployment_container_security_context_hardened():
    objects, _, _ = _render()
    deployment = _by_kind(objects, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    sec = container["securityContext"]
    assert sec["allowPrivilegeEscalation"] is False
    assert sec["capabilities"]["drop"] == ["ALL"]


def test_pod_has_seccomp_runtime_default():
    objects, _, _ = _render()
    pod_spec = _by_kind(objects, "Deployment")["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}


def test_container_read_only_rootfs_by_default_with_writable_tmp():
    objects, _, _ = _render()
    pod_spec = _by_kind(objects, "Deployment")["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    # A writable /tmp emptyDir + /data PVC are the only writable paths.
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts.get("tmp") == "/tmp"
    vols = {v["name"] for v in pod_spec["volumes"]}
    assert "tmp" in vols and "data" in vols


def test_read_only_rootfs_can_be_disabled():
    objects, _, _ = _render(Settings(provisioner_mode="kubernetes", nodered_read_only_root_fs=False))
    container = _by_kind(objects, "Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is False


def test_catch_all_egress_excepts_metadata_and_internal():
    # If an operator opens egress to 0.0.0.0/0, the metadata IP + internal ranges are subtracted.
    objects, _, _ = _render(
        Settings(provisioner_mode="kubernetes", nodered_egress_allow_cidrs="0.0.0.0/0")
    )
    egress = _by_kind(objects, "NetworkPolicy")["spec"]["egress"]
    catch_all = [
        peer["ipBlock"]
        for rule in egress
        for peer in rule.get("to", [])
        if peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"
    ]
    assert len(catch_all) == 1
    assert "169.254.169.254/32" in catch_all[0]["except"]


# ── NetworkPolicy: egress-deny + bridge-only ingress ─────────────────────────────
def test_netpol_policy_types_cover_ingress_and_egress():
    objects, _, _ = _render()
    netpol = _by_kind(objects, "NetworkPolicy")
    assert set(netpol["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert netpol["spec"]["podSelector"] == {"matchLabels": {"app": _render()[1]}}


def test_netpol_default_egress_allows_only_dns():
    # Default (no allow-list CIDRs): egress permits ONLY DNS, and nothing else —
    # no internal-platform CIDR, no 0.0.0.0/0 catch-all.
    objects, _, _ = _render(Settings(provisioner_mode="kubernetes", nodered_egress_allow_cidrs=""))
    netpol = _by_kind(objects, "NetworkPolicy")
    egress = netpol["spec"]["egress"]

    assert len(egress) == 1
    dns_rule = egress[0]
    # DNS rule targets no peer (``to: []``) and only ports 53/UDP + 53/TCP.
    assert dns_rule["to"] == []
    ports = {(p["protocol"], p["port"]) for p in dns_rule["ports"]}
    assert ports == {("UDP", 53), ("TCP", 53)}

    # No ipBlock peer anywhere by default; in particular no 0.0.0.0/0 catch-all.
    all_cidrs = [
        peer["ipBlock"]["cidr"]
        for rule in egress
        for peer in rule.get("to", [])
        if "ipBlock" in peer
    ]
    assert all_cidrs == []
    assert "0.0.0.0/0" not in all_cidrs


def test_netpol_ingress_restricted_to_bridge():
    objects, _, settings = _render()
    netpol = _by_kind(objects, "NetworkPolicy")
    ingress = netpol["spec"]["ingress"]

    assert len(ingress) == 1
    rule = ingress[0]
    assert rule["from"] == [{"podSelector": {"matchLabels": {"app": "tool-flow-bridge"}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": settings.nodered_container_port}]


def test_netpol_egress_appends_explicit_allow_cidrs_after_dns():
    # An explicit allow-list adds ipBlock peers AFTER the DNS rule; DNS stays first.
    settings = Settings(
        provisioner_mode="kubernetes",
        nodered_egress_allow_cidrs="203.0.113.0/24, 198.51.100.7/32",
    )
    objects, _, _ = _render(settings)
    netpol = _by_kind(objects, "NetworkPolicy")
    egress = netpol["spec"]["egress"]

    assert egress[0]["to"] == []  # DNS still first
    cidrs = [
        peer["ipBlock"]["cidr"]
        for rule in egress[1:]
        for peer in rule["to"]
        if "ipBlock" in peer
    ]
    assert cidrs == ["203.0.113.0/24", "198.51.100.7/32"]
    assert "0.0.0.0/0" not in cidrs
