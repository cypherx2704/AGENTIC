# smoketest/k8s

Kubernetes manifests for the Phase 1 Component 21 infra smoke test. There are
two deploy paths; the script uses whichever tooling is available, **preferring
Helm**.

## Preferred: Helm via the base chart (recommended)

`infra/smoketest/Chart.yaml` + `infra/smoketest/values.yaml` deploy echo-service
through the `charts/cypherx-service` base chart, which renders the Deployment,
Service, ServiceAccount, DopplerSecret, ServiceMonitor, and NetworkPolicy with
all Phase 0/1 contracts baked in (Contracts 6/7/8/13/14). This is the same
pattern every real service uses, so the smoke test exercises the real chart.

```bash
helm dependency build infra/smoketest
helm upgrade --install echo infra/smoketest -n smoketest --create-namespace \
  --set cypherx-service.image.repository=<ECR>/cypherx/echo-service \
  --set cypherx-service.image.tag=<git-sha7> \
  --set-string cypherx-service.extraEnv[7].value=<kafka-brokers>
```

The namespace label `istio-injection=enabled` and the Kong route are applied
from the raw manifests here (`namespace.yaml`, `kong-route.yaml`) because they
are not part of the per-service chart.

## Fallback: raw manifests (Helm-less)

For environments without Helm, `kubectl apply` the raw set. Keep these in sync
with `values.yaml`.

| File | Object | Backs |
|---|---|---|
| `namespace.yaml` | `smoketest` ns, Istio-injected | assertions 1, 4, 10 |
| `dopplersecret.yaml` | test DopplerSecret `echo-runtime` | assertion 9 |
| `echo-deployment.yaml` | Deployment + Service + ServiceAccount | assertions 1, 7, 8 |
| `servicemonitor.yaml` | ServiceMonitor (`job=echo`) | assertion 6 |
| `kong-route.yaml` | Kong Ingress for `/echo` | assertion 1 |

```bash
kubectl apply -f infra/smoketest/k8s/namespace.yaml
kubectl apply -f infra/smoketest/k8s/dopplersecret.yaml      # wait for sync
sed -e "s#REPLACE_ME_ECR#$ECR#" -e "s#REPLACE_ME_TAG#$TAG#" \
    -e "s#REPLACE_ME_KAFKA_BROKERS#$KAFKA_BROKERS#" \
    infra/smoketest/k8s/echo-deployment.yaml | kubectl apply -f -
kubectl apply -f infra/smoketest/k8s/servicemonitor.yaml
kubectl apply -f infra/smoketest/k8s/kong-route.yaml
```

Teardown is always `kubectl delete ns smoketest` plus the Kafka-topic / IAM /
ALB-target-group sweep in `infra/scripts/infra-smoke-test.sh` (assertion 10).
