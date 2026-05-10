# Security Policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in Kubeport, **do not open a public GitHub issue**. Instead, send a private report to the project maintainer:

- Email: `jcorfer910@g.educaand.es`
- Subject line: `[Kubeport security] <short description>`

Please include:

- A clear description of the vulnerability.
- A reproduction (commands, DocType operations, manifest snippets, or cluster conditions).
- The affected version (commit SHA or tag).
- The impact you observed and any suggested mitigation.

You should expect an acknowledgement within a few business days. Coordinated disclosure is preferred — please give the maintainer a reasonable window to release a fix before publishing details.

## Scope

In scope:

- The Kubeport Frappe app itself (`kubeport/` package, hooks, DocTypes, API endpoints, background tasks, scheduled jobs).
- The publish-site-image workflow (`.github/workflows/publish-site-image.yml`) and the catalogue script (`scripts/update_site_catalog.py`).
- The default site-image catalogue (`kubeport/site_images/catalog.json`).

Out of scope:

- Vulnerabilities in upstream Frappe / ERPNext, the Kubernetes Python client, the Helm CLI, or other dependencies — please report those to their respective maintainers.
- Cluster misconfiguration in the operator's own environment (RBAC, network policies, image-pull credentials).
- Denial-of-service via legitimately authenticated System Manager actions.

## Trust model and known boundaries

For the per-boundary STRIDE catalogue, the endpoint × boundary mapping, and the justification of the destructive-operation allowlist, see [`docs/threat-model.md`](docs/threat-model.md).

Kubeport is designed to be operated by users holding the Frappe `System Manager` role. The following are deliberate trust boundaries operators should be aware of:

- **Cluster credentials** stored in `Kubernetes Cluster` rows (kubeconfig content, bearer tokens, CA certificates) live in MariaDB. Anyone who can read those rows from the database can act as the cluster.
- **Bearer-token auth** without a CA certificate is rejected by default. The dev-only TLS bypass is explicitly named "dev-only" — never enable it in production.
- **Helm operations** are executed by an out-of-process `helm` binary on the bench host. Compromising that binary or its `PATH` compromises the control plane.
- **`Service Bundle`** validates manifests against a fixed allowlist of built-in resource kinds. CRDs and arbitrary custom resources are intentionally not supported.
- **`Kubernetes Command`** is the only doctype that exposes ad-hoc cluster operations. Delete is restricted to `Pod`, `Job`, and `ConfigMap`; destructive changes to `Secret`, `PVC`, `Deployment`, and `StatefulSet` must go through their dedicated controllers.
- **`Kubernetes Command Audit Log`** is append-only and survives row deletion. Rotate / archive it according to your retention policy.
- **Site image catalogue** rows can be `is_curated=1` (owned by the daily catalogue sync) or `is_curated=0` (user-registered). The curated set is digest-pinned. User rows may be created without a digest and then deploy by tag — that is an explicit operator choice.
- **Frappe Site credentials** flow into Kubernetes through per-Job Secrets that are owner-referenced to the Job (so they GC with the Job's TTL). They are never written to plaintext env vars on the Pod spec.

## Hardening recommendations

- Restrict who holds the `System Manager` role to operators who are authorised to manage the connected clusters.
- Run the bench under a dedicated Kubernetes service account whose RBAC is the minimum required by Kubeport's tasks (Helm releases, Jobs in target namespaces, the resource kinds in the `Service Bundle` allowlist, exec into bench pods).
- Mount only the kubeconfigs / tokens needed by the bench; do not co-locate unrelated cluster credentials in MariaDB.
- Keep the Helm binary on `PATH` pinned to a known version; consider packaging it with the bench image to avoid supply-chain drift.
- Monitor the `Kubernetes Command Audit Log` for unexpected delete activity.
