# Azure Security Baseline

This baseline applies to hosted AI Bench deployments. Local offline mode remains the
default and needs no Azure identity.

## Identity and provider authentication

- Azure-hosted workloads use a system-assigned or explicitly selected user-assigned
  managed identity.
- Production code uses `ManagedIdentityCredential`, not a broad fallback chain.
- Local Entra development may use `DefaultAzureCredential`.
- Assign `Cognitive Services User` to the workload identity at the actual Microsoft
  Foundry account scope, not merely at an application resource group.
- An existing Foundry account may live in another resource group. Deployment parameters
  identify its subscription, resource group, and account name without recreating it.
- Use the account's actual custom subdomain/endpoint returned by Azure. Do not construct
  an endpoint from a deployment name or resource-group convention.
- Role assignment is eventually consistent. Deployment and verification use a bounded
  propagation wait with diagnostics; they never retry forever.

## Entra mode and explicit key fallback

`entra` is the hosted default.

- Entra mode does not list, read, copy, persist, or expose provider keys.
- App settings contain an endpoint and authentication mode, not an API key.
- Key mode is an explicit local/compatibility fallback and requires an operator-supplied
  secret value.
- Switching to Entra mode removes the stale provider-key secret from Key Vault without
  reading its value.
- Because Key Vault uses soft delete, switching back to key mode first detects and
  recovers the deleted secret when necessary, then writes the newly supplied value.
- A recovered old value is never silently reused. Key mode requires a current value.
- Mode synchronization fails loudly if deletion, recovery, or write does not complete.
- Because the vault has no public endpoint, every transition into or out of key mode
  must run from a trusted host with private network and DNS access to the vault.
- A fresh target vault skips data-plane cleanup only after its exact ARM lookup reports
  ResourceNotFound and no soft-deleted vault has that name. An existing RG's AZD tag is
  not prior key-mode history. Permission/network/ambiguous lookup errors are `unknown`,
  never `fresh`. Existing deployments retain the exact target vault name. AZD records a non-secret
  mode-state marker before key writes so interrupted transitions cannot be mistaken for
  a fresh vault.
- The operator must set
  `AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS=true` only after verifying private
  network and DNS connectivity; preflight blocks key-mode work and uncertain
  migrations without that assertion.
- Mode synchronization temporarily gives the operator `Key Vault Secrets Officer` for
  data-plane deletion, recovery, or replacement. A deterministic assignment identity
  lets an interrupted retry reclaim and remove only the assignment the script owns,
  including on propagation timeout or operator interruption. Pre-existing operator
  assignments are preserved.

## Storage

- Disable public network access.
- Disable blob public access and shared-key authorization.
- Require secure transfer and TLS 1.2 or later.
- Set OAuth as the default authentication model.
- Use separate private endpoints and private DNS for Blob and Table. Queue is not an
  active execution/readiness dependency and is not provisioned for this release.
- Assign data-plane roles at the narrowest useful scope. The application should not
  receive account-key listing permissions.
- Keep immutable manifests and evidence protected by conditional writes/version checks.
- Hosted execution explicitly selects Azure Table coordination and private Blob
  evidence, with separate evidence/staging containers and a precreated Table. Assign
  Blob Data Contributor twice at the exact container scopes and Table Data Contributor
  at the exact Table scope, not account-wide. `/home` is disposable cache/operator
  staging, never authoritative SQLite coordination.
- One Linux B1 instance and a fenced supervisor remain mandatory. Real Table/Blob
  conformance and an operator-reviewed promotion attestation precede network enablement.
- Ten-day upload/derived retention is enforced by the core ownership-aware reaper.
  Disable Blob/container soft delete and versioning on the **new app storage only**;
  refuse an in-place recovery-policy reset on unrelated existing storage. Never use
  account-wide age deletion for shared/pinned CAS content. Installed dataset and
  designated original baseline stay pinned; ordinary later runs still expire.

## Key Vault

- Disable public network access.
- Use the RBAC authorization model.
- Enable soft delete and purge protection.
- Use a private endpoint and private DNS.
- Assign `Key Vault Secrets User` only when explicit key mode needs secret access.
- Remove stale provider secrets through the supported Key Vault data-plane delete
  operation without retrieving their values when changing to Entra or offline mode.
- Do not put secrets in Bicep parameters, AZD environment output, source settings, logs,
  deployment packages, or test fixtures.

## Network

- Integrate App Service outbound traffic with a delegated VNet subnet.
- Use a distinct subnet for private endpoints.
- Link all required private DNS zones to the VNet.
- Route private service traffic through VNet integration.
- App ingress is HTTPS-only. Generic restricted mode still requires an operator CIDR
  before networked execution. The approved public demonstration is a deliberate
  `AI_BENCH_PUBLIC_DEMO_MODE=true` exception with no login/Easy Auth/CAPTCHA/ownership
  gate or cumulative spend cap, Entra provider identity and explicit per-run consent.
  Default local/offline and restricted modes are not weakened.
- SCM never inherits public main-site ingress. Restricted defaults use an independent
  operator-CIDR/default-deny policy. The explicit MVP exception requires public-demo
  Entra mode and an explicitly empty SCM CIDR: network-reachable SCM still requires
  Entra/RBAC deployment authentication; SCM/FTP basic publishing remains disabled.
  Missing configuration must not imply this exception. Verify anonymous deployment
  access is denied (401 or an actual Entra redirect), not just blocked by an IP rule.
  Main-site public access does not grant deployment or operator privileges.
- Public uploads reserve count/aggregate capacity with small admission JSON, transfer
  each bounded file through an independently idempotent app-proxied PUT, then explicitly
  finalize before asynchronous validation/publication. Keep the total dataset budget
  at 250,000,000 bytes without requiring a dataset-sized HTTP request. No browser SAS
  or direct private-storage redirect is permitted.
- Both existing AI accounts use the explicitly approved public-network posture:
  `publicNetworkAccess=Enabled`, `networkAcls.defaultAction=Allow`, and no IP-rule
  allowlist (`ipRules=[]`). This enables portal/browser/playground and App Service
  network reachability, **not** anonymous inference. Managed identity/Entra and
  `disableLocalAuth=true` remain mandatory.
- Do not provision AI private endpoints or AI DNS zones/links. The only three private
  endpoints/NICs and three VNet-linked private DNS zones are Blob, Table and Key Vault.
  Their public-access restrictions remain unchanged.
- This baseline does not provision App Service or SCM private endpoints. Preflight
  therefore rejects `AI_BENCH_ENABLE_APP_PUBLIC_ACCESS=false`; add both private
  endpoints and their private DNS before selecting private-only ingress.
- Disable FTPS and require TLS 1.2 or later.

## Resource preservation

- When an existing resource group is selected, do not redeclare it or replace its tags.
- The hosted entrypoint accepts only an existing resource group; zero groups, AI
  accounts, model deployments or child projects are created/replaced.
- Preserve optional existing configuration when a redeploy parameter is omitted. Do not
  replace an existing Foundry account, custom subdomain, app setting, retention value,
  tag, or network setting with an empty default.
- Use deterministic role-assignment names and incremental, idempotent deployments.
- Auth-mode cleanup deletes only its exact operation-owned assignment IDs, never every
  assignment with a matching role name. Before/after preservation snapshots retain
  group tags, account settings, models, projects and all original operator roles.
- The only approved account update is the three-field public-network change.
  Freeze actual ARM read-back writable settings, reject unknown properties or stale
  snapshots, preserve identity/encryption/SKU/location/tags/subdomains/projects and
  every other account property, and guard the update with an existing-resource read.
  The change must appear in the main Bicep what-if as resolved `Modify` (or an already
  applied `NoChange`), never Create/Replace/Delete/Ignore. No hidden posthook PATCH.
  Preprovision rechecks drift; concurrent account changes require refreshed review,
  not an unconditional PUT of defaults. Do not deploy the update module standalone.

## Subscription preflight

Before deployment, verify:

1. The target subscription and location are explicitly confirmed.
2. Required resource providers are registered, or the operator has
   `Microsoft.Resources/subscriptions/providers/register/action`.
3. The deployer can create role assignments, which requires
   `Microsoft.Authorization/roleAssignments/write` (normally User Access Administrator,
   Role Based Access Control Administrator, Owner, or a suitable custom role).
4. The deployer can read each exact existing account and has
   `Microsoft.CognitiveServices/accounts/write` plus role-assignment permission at
   both actual account scopes for the reviewed network-only update.
5. Required regional quota and service availability are sufficient.

`Contributor` alone cannot create role assignments. Preflight errors must name the
missing action and scope so an administrator can grant only what is needed. All
subscription-scoped checks use the AZD-selected `AZURE_SUBSCRIPTION_ID`, not the Azure
CLI default. Microsoft Graph identity resolution is requested in that selected
subscription's tenant, and user checks include transitive group-granted role
assignments.

## Deployment verification

Do not trust delayed or incomplete AZD tracking. Verification checks:

- ARM reports one Linux instance, exact identity/role-definition IDs and scopes,
  approved/provisioned PEs in the app subnet, complete DNS zone groups/VNet links,
  independent SCM and actual soft-delete/versioning policy. Role names or PE target
  IDs alone are not sufficient.
- The application URL responds over HTTPS.
- The SCM/Kudu endpoint is reachable by an authorized operator.
- `/health` proves the process is alive.
- `/ready` proves required storage and configuration dependencies are ready.
- Backend qualification validates its genuine immutable object/marker, matching
  configuration, backend/identity, supported versioned suite and all required scenarios.
  Readiness is not exact-build hash equality. Source/dependency hashes remain build
  provenance and publication checks; significant backend releases still require
  genuine exact-build conformance. The older 17-case proof does not satisfy the
  current 21-case parallel contract. Unknown/inadequate proofs fail closed.
- App-runtime evidence (not the separate Kudu container) proves managed-identity-only
  tokens, private Blob/Table DNS/TLS, public provider DNS/TLS, and real Blob/Table
  conformance before enablement. Independently prove both provider accounts are
  reachable from the portal/browser/playground with Entra authorization still required.
  No provider key, storage key or SAS is read.
- Separately authorized paid proof obtains an Entra token and performs real calls.
- A proof benchmark run uses a declared networked solution, reaches terminal completion,
  and exposes its immutable manifest, attempt evidence, telemetry, metrics, and report.
- Verification downloads the manifest, report, task evidence, and attempt evidence;
  checks every content SHA-256 against its artifact URL; confirms run/manifest/report
  identity agreement; and requires a provider request identifier.

A deployment without the real provider call and completed evaluation is not proven.
For this public release it also requires baseline archive/hash verification,
two-pair/five-solution proof, restart persistence, native playback/download, ten-day
expiry/reaper and browser evidence. Ordinary validation is read-only and must never
silently run import, restart, mode synchronization, conformance writes or paid proof.
Provider dispatch uses the approved availability policy: one current-generation
coordinator, with at most five concurrent tasks across opted-in runs and one per
pack/solution identity. Historical/other serial runs remain serial; shared provider
cooldowns are not split to increase concurrency. Only the owner thread admits work;
loss of readiness, leadership or authorization stops new provider admission. In-flight
requests drain or follow durable recovery; no replacement pool overlaps in the same
process. Rare duplicate or overlapping external requests remain possible after
crashes, pause-before-POST/takeover or lost responses. Persist request-start and
indeterminate usage evidence; fencing rejects stale canonical/result writes while
the current generation continues with the hosted three-attempt ceiling and durable
cooldowns. Table fencing is not proof of physical non-overlap or exactly-once calls.
Adversarial takeover/lost-response regressions must verify these bounded-risk
semantics rather than assert an impossible physical single-call guarantee.

## Logging and telemetry

- Send application and platform diagnostics to Log Analytics and Application Insights.
- Never send uploads, transcript/reference text, filenames, content-bearing artifact
  URLs, authorization headers, keys or signed URLs into monitoring. Platform diagnostic
  collection excludes content-bearing HTTP/console categories.
- Retain provider request IDs, safe error classifications, latency, usage, retry, and
  correlation identifiers.
- Treat monitoring public ingestion/query settings as explicit deployment decisions.
- The baseline leaves Azure Monitor ingestion and query public because it does not
  provision an Azure Monitor Private Link Scope (AMPLS). Setting
  `AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS=false` is valid only after extending the
  deployment with AMPLS, its private endpoint, and the required private DNS zones;
  otherwise application telemetry cannot reach Application Insights.
