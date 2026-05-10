#!/usr/bin/env bash
# kubectl auth can-i smoke for the Kubeport ServiceAccount.
#
# Probes every verb-resource pair Kubeport actually calls (one per audited
# call site under kubeport/utils/{k8s_resources,discovery,observability}.py
# and kubeport/tasks/*.py). Reports PASS / FAIL per probe and exits non-zero
# on any failure.
#
# Usage:
#   make rbac-smoke
#   KUBEPORT_NAMESPACE=other-ns deploy/rbac/smoke.sh
#   KUBEPORT_PROBE_NAMESPACE=workload-ns deploy/rbac/smoke.sh
#
# Requirements:
#   - kubectl on PATH and KUBECONFIG pointing at the target cluster.
#   - Caller has permission to impersonate the kubeport ServiceAccount
#     (typically cluster-admin); without it `--as=...` is denied at the
#     impersonation admission stage and every probe reports FAIL.
#
# Environment:
#   KUBEPORT_NAMESPACE        Namespace where the kubeport SA is bound (default: kubeport-system).
#   KUBEPORT_PROBE_NAMESPACE  Namespace passed as `-n` for namespaced probes (default: default).

set -uo pipefail

NS="${KUBEPORT_NAMESPACE:-kubeport-system}"
SA="system:serviceaccount:${NS}:kubeport"
PROBE_NS="${KUBEPORT_PROBE_NAMESPACE:-default}"

if ! command -v kubectl >/dev/null 2>&1; then
	echo "kubectl is required on PATH" >&2
	exit 2
fi

pass=0
fail=0
fails=()

probe() {
	# probe <verb> <resource> [scope]
	#   scope = "namespaced" (default) or "cluster"
	local verb="$1" resource="$2" scope="${3:-namespaced}"
	local cmd=(kubectl auth can-i "$verb" "$resource" "--as=$SA")
	if [[ "$scope" == "namespaced" ]]; then
		cmd+=(-n "$PROBE_NS")
	fi
	if "${cmd[@]}" >/dev/null 2>&1; then
		printf 'PASS  %-7s %-32s (%s)\n' "$verb" "$resource" "$scope"
		pass=$((pass + 1))
	else
		printf 'FAIL  %-7s %-32s (%s)\n' "$verb" "$resource" "$scope"
		fails+=("$verb $resource ($scope)")
		fail=$((fail + 1))
	fi
}

echo "Probing as ${SA}"
echo "  namespaced verbs scoped to ${PROBE_NS}"
echo "----"

# --- core/v1 namespaced (Service Bundle apply + Frappe Site creds Secret +
# Helm storage Secrets + bench discovery + readiness reads) ----------------
for kind in pods services configmaps secrets persistentvolumeclaims serviceaccounts; do
	for verb in get list create patch delete; do
		probe "$verb" "$kind"
	done
done

# Pod log drilldown (observability.py) and pod exec (discovery + site probe).
probe get  "pods/log"
probe create "pods/exec"

# Event drilldown reads both core and events.k8s.io.
probe list "events"
probe list "events.events.k8s.io"

# --- core/v1 cluster (Service Bundle Namespace apply, orphan-Job sweep) ---
for verb in get list create patch delete; do
	probe "$verb" "namespaces" cluster
done

# --- apps/v1 (SB apply + readiness walk + rollout-context reads) ----------
for kind in deployments.apps statefulsets.apps daemonsets.apps; do
	for verb in get list create patch delete; do
		probe "$verb" "$kind"
	done
done
for kind in replicasets.apps controllerrevisions.apps; do
	probe get  "$kind"
	probe list "$kind"
done

# --- batch/v1 (Frappe Site lifecycle Jobs + SB CronJob) -------------------
for kind in jobs.batch cronjobs.batch; do
	for verb in get list create patch delete; do
		probe "$verb" "$kind"
	done
done

# --- networking.k8s.io (Service Bundle Ingress apply + readiness walk) ----
for verb in get list create patch delete; do
	probe "$verb" "ingresses.networking.k8s.io"
done

# --- rbac.authorization.k8s.io namespaced (Service Bundle apply) ----------
for kind in roles.rbac.authorization.k8s.io rolebindings.rbac.authorization.k8s.io; do
	for verb in get list create patch delete; do
		probe "$verb" "$kind"
	done
done

# --- rbac.authorization.k8s.io cluster (Service Bundle apply) -------------
for kind in clusterroles.rbac.authorization.k8s.io clusterrolebindings.rbac.authorization.k8s.io; do
	for verb in get list create patch delete; do
		probe "$verb" "$kind" cluster
	done
done

# --- storage.k8s.io (default-StorageClass discovery) ----------------------
probe list "storageclasses.storage.k8s.io" cluster

echo "----"
echo "PASS=${pass} FAIL=${fail}"
if (( fail > 0 )); then
	printf '  %s\n' "${fails[@]}" >&2
	exit 1
fi
