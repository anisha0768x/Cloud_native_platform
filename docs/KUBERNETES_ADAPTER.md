# Optional existing-Kubernetes adapter

**Validation status: adapter logic tested; no live cluster available in the validation environment.** Docker is the verified default. This guide connects the capacity controller to a Kubernetes workload; it does not deploy all twelve control-plane services into Kubernetes.

The controller needs an HTTPS Kubernetes API URL reachable from its container, the cluster CA file, a service-account bearer-token file, and a reachable workload HTTP endpoint. It reads the token file for every API request so a rotated token can be supplied without restarting the application.

1. Build `helio-worker:v3` and make it available to your cluster, by loading it into a local cluster or publishing it to your registry. Update the manifest image if using a registry. Do not assume your cluster sees Docker Desktop's local image cache.
2. Review and apply `distributed/infra/kubernetes-workload.yaml`. It creates only a dedicated namespace, one workload deployment/service and a narrowly scoped controller service account/Role/RoleBinding.
3. Obtain a short-lived token for `helio-capacity` and save it locally. Keep credentials out of source control:

```powershell
New-Item -ItemType Directory -Force .secrets
kubectl -n helio-workload create token helio-capacity --duration=1h | Set-Content -Encoding ascii .secrets/kube-token
```

4. Save the cluster's trusted CA certificate as `.secrets/kube-ca.crt`. Use your actual cluster configuration; do not disable TLS verification. Protect these files and renew the token before expiry.
5. Generate the adapter configuration using real reachable addresses. These example values are placeholders:

```powershell
$apiUrl = 'https://YOUR-KUBERNETES-API:6443'
$workloadUrl = 'http://YOUR-REACHABLE-NODE:30090'
python distributed/scripts/kubernetes_config.py --api-url $apiUrl --workload-url $workloadUrl --token-file .secrets/kube-token --ca-file .secrets/kube-ca.crt
docker compose --env-file distributed/.env -f distributed/compose.kubernetes.generated.yaml up -d --no-build
```

The generated file removes the Docker socket from the capacity container. Continue using this generated Compose file while using Kubernetes; the normal launcher selects the Docker configuration.

The role can read only `checkout-api` and patch its scale subresource. It cannot list secrets, manage nodes, create deployments or scale unrelated deployments.

Scaling is pending until the controller has observed the deployment generation and updated/ready/available replica counts match the desired count, with a healthy workload endpoint. If the API/token/CA/endpoint is wrong, the website reports the connection problem; no synthetic cluster state is substituted.

Self-healing of Kubernetes pods is provided by the deployment controller and workload probes. The application's Docker replacement loop does not delete Kubernetes pods. The workload NodePort is for a controlled lab network; apply suitable network restrictions before wider exposure.
