# Azure Deployment

AI Bench uses Azure Developer CLI with Bicep. The template is safe to build and test
without Azure; live deployment is an explicit operator action.

**MVP release boundary:** one explicitly authorized, authenticated app-only release.
Use the local command below after exact-source acceptance and green main CI.
Provisioning, archive import and paid provider proof still require separate
authorization; the release command never performs them. Never deploy
`infra/speech-probe.bicep` as part of the hosted path.

## One local MVP release command

Use the existing Azure CLI/AZD and GitHub CLI identity, selected AZD environment and
an isolated Python environment. No new runner, OIDC identity or infrastructure is
required. Install `requirements-operator.lock` into that virtual environment only
when operator dependencies are absent; Paramiko is the local authenticated
SSH/SFTP transport, not an application dependency.

The checkout must be the accepted current default-branch commit with successful
exact-head CI and clean source (the local deployment plan is excluded). First run
the same command without `--execute` for a strictly read-only preview:

```powershell
python -m infra.scripts.release --scm-entra-only `
  --conformance-namespace <approved-empty-conformance-namespace> `
  --evidence-dir <new-external-evidence-directory>
```

After release approval, add `--execute`. This single command checks target/auth,
idleness, package/Git byte identity, SCM Entra protection, worker identity and
private-storage connectivity; stages the **complete** candidate privately; runs
required zero-inference conformance; deploys the exact frozen ZIP once; and verifies
actual serving source/dependencies, health/readiness, original report/manifest hashes
and precise configuration/resource preservation. Evidence stays outside source, with
no full historical artifact download at each step.

The release path deploys application code only. Provider registration, quota,
capacity and role-assignment preflight belong to provisioning, which `azd deploy`
cannot change, so `infra.scripts.preflight` and a pre-deploy
`infra.scripts.verify_deployment` run are not part of a release. Run those for
infrastructure work as described below. Live ARM state, Kudu, settings, resources
and readiness are still verified after the deployment, and a release that fails
that verification fails closed.

`--scm-entra-only` explicitly selects the MVP network policy: public-demo plus Entra
and an **explicit empty** `AI_BENCH_SCM_ALLOWED_CIDR`. The command applies only the
three SCM network properties with a narrow configuration PATCH; it does not modify
main ingress, storage, Vault, identity, basic publishing auth, runtime or capacity.
It requires authenticated SCM access and either anonymous HTTP 401 or a verified
Entra login redirect, without following redirects or forwarding bearer tokens.
Restricted deployments retain a nonempty canonical bounded CIDR and default deny.
An omitted value is not permission to open SCM. An already matching policy needs
no repeated network mutation.

The dry run performs reads only: no build/hook execution, local output directory,
SSH/SFTP, namespace writes, configuration updates, restart or deployment. It reports
that real candidate qualification will be determined during explicit execution;
it does not falsely claim to have run it.

For execution, the evidence directory must be new. The operation retains source/CI,
before-state fingerprints, ZIP/per-file hashes, actual worker Python/SDK versions,
qualified scenarios/digest, deployment start/result and after-state checks. A lost
deployment response is reconciled read-only with Kudu and serving identity. The
identical qualified ZIP is resent only when an interrupted build left an orphaned
Kudu deployment lock, which is released by exact ETag before a bounded retry; a
candidate that Kudu actually rejected is never resent. An unresolved failure stops
with its evidence.
Do not rerun destructive conformance against its now-nonempty namespace; review a
failed operation before deciding the next action. No automatic cleanup is performed.

### Stable runtime qualification versus build qualification

The runtime checks genuine content hash, published Table marker, configuration,
backend/identity, supported `hosted-core-v1` suite and **all 21 required scenarios**.
Exact source/dependency hash equality is deliberately not a readiness condition:
a compatible UI/app rebuild may reuse its existing qualification.
Code/dependency hashes remain required at proof publication and in release evidence.
Unknown schemas/suites, wrong configuration/identity, absent/corrupt content/marker
and missing scenarios fail closed; there is no bypass switch.

Historical 17-case attestations remain readable but **do not qualify this runtime**.
The synthetic parallel probe holds its slow invocation until the controller witnesses
durable fast-lane completion while that invocation is still executing. One 120-second
overall watchdog bounds all observation phases; individual lanes cannot expire their
own barriers. Watchdog/cleanup releases are failures, never successful progress.
The initial parallel release must run all current 21 genuine Azure scenarios in
the approved fresh namespace and automatically apply the resulting digest once,
then deploy. Its old-package/new-proof transition may briefly fail readiness and
the one app-setting update may restart the app. Later compatible releases retain
that setting. Changes to signature-covered backend sources, the qualification
operator or locked runtime dependencies require exact-build release qualification
even when protocol readiness remains compatible. Unrelated UI changes do not.
Dependency-lock migration currently stops for a matched candidate environment
rather than silently qualifying old installed dependencies.

Treat suite versions as reviewed compatibility boundaries: incompatible storage,
fencing, lease or recovery semantics require a suite-version bump and a new genuine
qualification before activation. Added mandatory scenarios invalidate inadequate
older proofs explicitly. Do not infer compatibility from a real cloud digest,
hardcode an environment ID, remove scenarios or exclude sources to make readiness
pass. No legacy 17-to-21 equivalence mapping exists.

## Prerequisites

- Azure CLI and Azure Developer CLI
- Bicep CLI available through Azure CLI
- Permission to create the declared resources
- Permission to register required providers, or providers already registered
- `Microsoft.Authorization/roleAssignments/write` at every role-assignment scope
- Read access to an existing Foundry account when reusing one
- A confirmed subscription and region with sufficient quota

See [Azure security baseline](azure-security-baseline.md) before selecting parameters.

## Prepare

1. Run all local quality gates in [AGENTS.md](../AGENTS.md).
2. Sign in:

   ```powershell
   az login
   azd auth login
   ```

3. Create an AZD environment without committing its state:

   ```powershell
   azd env new
   azd env set AZURE_LOCATION swedencentral
   azd env set AI_BENCH_EXISTING_RESOURCE_GROUP <selected-existing-resource-group>
   azd env set AI_BENCH_AUTH_MODE entra
   azd env set AI_BENCH_FOUNDRY_SUBSCRIPTION_ID <subscription-id>
   azd env set AI_BENCH_FOUNDRY_RESOURCE_GROUP <resource-group>
   azd env set AI_BENCH_FOUNDRY_ACCOUNT <account-name>
   azd env set AI_BENCH_MAI_SUBSCRIPTION_ID <same-subscription-id>
   azd env set AI_BENCH_MAI_RESOURCE_GROUP <same-existing-resource-group>
   azd env set AI_BENCH_MAI_ACCOUNT <existing-mai-account>
   azd env set AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT <existing-deployment-name>
   azd env set AI_BENCH_WHISPER_DEPLOYMENT <existing-deployment-name>
   azd env set AI_BENCH_ENABLE_APP_PUBLIC_ACCESS true
   azd env set AI_BENCH_ALLOW_NETWORKED_SOLUTIONS false
   azd env set AI_BENCH_INGRESS_ALLOWED_CIDR ""
   azd env set AI_BENCH_SCM_ALLOWED_CIDR <operator-public-ip>/32
   azd env set AI_BENCH_PUBLIC_DEMO_MODE true
   azd env set AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS true
   azd env set AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS true
   azd env set AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS false
   ```

The template does not create an Azure Monitor Private Link Scope. Keep monitoring
public access enabled unless the deployment is extended with AMPLS, its private
endpoint, and the required private DNS zones. Disabling the setting by itself blocks
application telemetry ingestion.

4. Initialize behavior-safe defaults and run the read-only preflight:

   ```powershell
   python -m infra.scripts.prepare_environment
   ```

   This explicitly opts into the unauthenticated public demo, not a generic default.
   The initializer writes local AZD state, preserves explicit values and recovers
   omitted deployed policy/settings where unambiguous, then runs read-only preflight.
   It does not provision or synchronize authentication. `python -m infra.scripts.preflight`
   performs only the read-only checks after local preparation.

   A selected existing resource group is mandatory; its tags are never redeclared.
   New storage/vault names are deterministic local inputs, not inferred from the RG's
   historical AZD tag. Fresh-vault detection checks the exact target vault's ARM absence
   and excludes a soft-deleted vault. Permission/network errors remain `unknown`.
   Existing deployments must retain their exact vault name. Preflight refuses to alter
   recovery settings on unrelated or older storage accounts.

5. Review a deployment preview:

   ```powershell
   python -m infra.scripts.provider_network --preview <local-ignored-what-if.json>
   azd provision --preview
   ```

   The first command uses the same Bicep/AZD parameter inputs and requests
   `FullResourcePayloads` from ARM what-if. It retains local parameter/preview evidence
   and rejects unresolved or non-network account changes. It records only a local
   digest tying that successful check to the exact Bicep sources and parameter values.
   The AZD preprovision hook rejects a missing/stale digest. This does not substitute
   for human review or authorize provisioning; no resource is mutated by either preview.

Do not continue when preflight reports a missing provider, role-assignment action,
Foundry scope, quota, private-DNS dependency, or unresolved parameter.

Keep the actual subscription, group/account names, endpoint-map selectors, deployment
names, output IDs and all snapshots in local ignored configuration. Endpoints come
from `properties.endpoint` or an explicitly selected key in ARM `properties.endpoints`
using `AI_BENCH_SPEECH_ENDPOINT_KEY`, `AI_BENCH_OPENAI_ENDPOINT_KEY` and
`AI_BENCH_MAI_ENDPOINT_KEY`. Empty selects the actual account endpoint. Preflight rejects
a missing selected endpoint; it never constructs a hostname. Provider firewall changes
are exclusively the explicit Bicep updates described below, not a preflight/posthook PATCH.
Inspect the actual maps without changing local or cloud state:
`python -m infra.scripts.provider_network --inspect-endpoints`.
Do not guess service key names. A nonempty selector must exist and its exact value
must be an HTTPS custom account root; a bad key never falls back to `properties.endpoint`.
Regional hosts, userinfo/query strings and values already containing service or
project paths are rejected, not rewritten. The pack appends its declared Speech or
OpenAI route to the selected root. Neither map inspection, root validation nor a mock
test proves that `properties.endpoint` supports both services; real routing requires
the later separately authorized provider proof.
Hosted speech requires the complete five endpoint/deployment settings and Entra mode.
It rejects `AI_BENCH_SPEECH_PROVIDER_CONFIG` even when those settings are complete.
Preparation/preflight reject a stale local-file fallback without silently deleting
operator configuration; review its removal explicitly before redeployment.

## Reviewed resource composition

The existing group receives one Linux B1 plan/site (capacity one), one new StorageV2/LRS
account, private evidence/staging containers, one precreated coordination Table, one
VNet with dedicated integration/PE subnets, one private Key Vault and monitoring.
There is no Queue dependency, new group, AI account, AI project or model deployment.
Both existing S0 AIServices accounts, child project, tags, identity, encryption,
model pins/allocations, custom subdomains and disabled local auth are preserved.
The approved exception changes only `publicNetworkAccess` to `Enabled`,
`networkAcls.defaultAction` to `Allow`, and `networkAcls.ipRules` to `[]`.
Both accounts are publicly reachable for portal/browser/playground and App Service;
public networking does **not** enable anonymous inference or service keys.
Primary model pins are Whisper `001` Standard capacity 3 and
GPT Transcribe `2026-07-28` GlobalStandard capacity 10; MAI stays in East US.

Three private endpoints/NICs and three VNet-linked DNS zones serve Blob, Table and
Key Vault only. There are no AI private endpoints or AI private DNS zones/links.
Storage and Key Vault still have public access disabled; this provider-network
decision does not broaden either data store.

### Preservation-safe existing-account network updates

`prepare_environment` reads both exact existing accounts with ARM API `2025-06-01`
and freezes their actual observed and writable settings in the ignored local
`AI_BENCH_PROVIDER_ACCOUNT_SNAPSHOTS` value. Unknown properties, missing accounts,
unsupported restore/migration state and secret-bearing API properties fail closed.
Writable identity configuration retains its type and user-assigned identity IDs;
read-only principal/tenant IDs remain in the preservation snapshot, not the PUT body.
All other writable settings, including tags, SKU, account location, encryption,
custom subdomain, projects, outbound rules and capabilities' governing settings are
round-tripped rather than replaced with permissive defaults.
The undocumented `armFeatures` metadata is retained in both the observed snapshot and
the exact preserved account payload, rather than assuming it can be discarded because
the published schema omits it. Its recognized feature-name array must remain unchanged
in the resolved preview and post-deployment read-back. Unrecognized shapes still fail
closed. Account snapshots use AZD's file-based environment import with exact read-back
verification so large endpoint maps cannot exceed Windows command-line limits. The
snapshot value is base64-encoded JSON, decoded by Bicep, so AZD parameter substitution
cannot interpret embedded JSON quotes or dotenv variables. Encoding is not encryption;
these local snapshots still must not be committed.

`provider-public-network.bicep` is dependent on an existing-account read guard.
It applies the three approved network values over the frozen writable properties;
the updates are visible resources in the ordinary main Bicep deployment. There is
no hidden account PATCH in `postprovision` or another script. Account creation,
replacement, deletion, project/model changes and unrelated account drift are forbidden.
The read-only preview checker requires both accounts to be explicitly `Modify`
(or `NoChange` only when already at the approved target), with resolved before/after
payloads matching the exact approved delta. An `Ignore`/unresolved account preview is
not success.

Every preprovision rechecks the frozen snapshot against ARM; it accepts only the
original state or the exact already-applied network-only state. Concurrent edits stop
the operation. Use `python -m infra.scripts.provider_network --refresh-snapshot`
only to start a newly reviewed preview after legitimate drift; it does not update
Azure. Do not change accounts between approved preview/preflight and provisioning.
ARM template PUTs do not supply an account-level ETag lock; an external concurrent
account deletion/change during that window remains a reason to abort and re-review.
Never bypass the existence/read-back/Modify-only checks or deploy the update module
standalone.

Writable fields follow the [official account schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/2025-06-01/accounts).

The app receives exactly two container-scoped Blob Data Contributor assignments, one
Table-scoped Table Data Contributor, and Cognitive Services User on each actual account.
Entra-only mode grants no app Key Vault role. Unknown/pre-existing operator assignments
must never be removed by auth-mode cleanup.
Hosted role-assignment names use the stable App Service resource ID; their principal
property still uses the actual managed-identity object ID. This makes all five resource
IDs reviewable before the new identity exists. An identity replacement requires
explicitly reviewed assignment repair, never automatic deletion of existing roles.

Before preview/provision, capture the reviewed existing resource state outside tracked
files: `python -m infra.scripts.preservation --snapshot <local-evidence-file>`.
After deployment, `--compare <same-file>` checks group tags, account configuration,
models, projects, identity and the continued presence of every original role assignment.
Only account provisioning-state metadata is excluded. With explicit public-provider
enablement, comparison allows exactly the three approved account-network changes
and no others. Reject account/model/project drift, any other firewall broadening,
replacement, deletion or changes outside the selected group.
Recheck provider registration, account-scoped write and role-assignment permissions,
regional B1 capacity, quotas, policy and subnet address space. Prior quota observations
are evidence of planning, not a capacity reservation or successful ARM validation.

## Authentication modes

### Entra mode

Use `entra` for hosted workloads. Supply the existing Foundry account coordinates and
let Bicep assign `Cognitive Services User` at the account scope. The application uses
managed identity and the actual account endpoint. No provider key is read or stored.
Networked solutions remain disabled until the operator also sets
`AI_BENCH_ALLOW_NETWORKED_SOLUTIONS=true`.

Keep networked execution disabled while validating infrastructure. Enable it only after
the pack, provider identity, ingress controls, and proof solution have been reviewed.
Ordinary restricted deployments require a bounded main-site operator IPv4 CIDR before
networked execution can be enabled. The approved public release instead explicitly uses
`AI_BENCH_PUBLIC_DEMO_MODE=true`, Entra provider auth and an empty main ingress CIDR.
It has no Easy Auth/login/CAPTCHA/ownership/spend gate. SCM is independently
default-deny with a bounded `AI_BENCH_SCM_ALLOWED_CIDR`, or explicitly network-reachable
in public-demo Entra mode with an empty SCM CIDR and Entra deployment authentication;
SCM and FTP basic publishing credentials are disabled. Main ingress never controls SCM.
Every run still separately authorizes provider execution. For the restricted mode:

```powershell
azd env set AI_BENCH_INGRESS_ALLOWED_CIDR <operator-public-ip>/32
azd env set AI_BENCH_ALLOW_NETWORKED_SOLUTIONS true
```

### Explicit key mode

Key mode is a compatibility fallback. Supply a current key through the operator process,
never a tracked file. The post-provision mode-sync script recovers a soft-deleted secret
when needed and writes the supplied value. Switching back to Entra removes the stale
secret through the supported Key Vault data plane without reading it. Run every
transition into or out of key mode from a trusted host with private network and DNS
access to the vault. The script temporarily grants the deployer's actual user or service
principal `Key Vault Secrets Officer`, then removes only the assignment it created.
When `AZURE_PRINCIPAL_ID` is supplied explicitly, also set
`AZURE_PRINCIPAL_TYPE` to `User` or `ServicePrincipal`.

After confirming that the operator host resolves and reaches the vault through the
deployment VNet, explicitly acknowledge that prerequisite:

```powershell
azd env set AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS true
```

The first Entra/offline deployment of an exactly absent, non-soft-deleted target vault
skips data-plane cleanup even when its reused group already has an AZD tag. AZD maintains
`AI_BENCH_KEY_VAULT_MODE_STATE` as non-secret local deployment state. Existing or
recovered environments without that marker are treated as unknown and require private
cleanup access rather than assuming no stale secret exists. Do not edit the marker
manually.

## Provision and deploy

After preview and approval:

```powershell
azd provision
azd deploy
```

The AZD `prepackage` hook actually runs the normal frontend build, compares its generated
assets to the reviewed tree, and rejects drift before staging the code allowlist.
It does not install packages. Restore dependencies only after a missing-dependency
failure or dependency change, within the prepared environment. A clean checkout never
depends on a pre-existing `infra/package` directory. Archive bundles, publications,
user uploads, provider JSON/config, environment state and results must never enter
that directory. Only the reviewed operator connectivity probe is included alongside
application code, not operator evidence or datasets.
The `postdeploy` hook uses explicit read-only `verify_deployment --bootstrap`: it checks
ARM/SCM/health and the selected Azure runtime, and clearly reports **not deployment
proof**. It does not bypass the application's unpromoted/baseline `/ready` gate, run
conformance, import archives or enable providers. Ordinary verification below still
requires `/ready` after the operator setup completes.

AZD environment files, package staging, and deployment outputs are local state and must
not be committed.

The baseline does not include App Service or SCM private endpoints, so
`AI_BENCH_ENABLE_APP_PUBLIC_ACCESS=false` is rejected by preflight. Keep HTTPS ingress
enabled and bounded by the operator CIDR when networked execution is enabled, or extend
the infrastructure with both private endpoints and private DNS first.

## Verification

Run:

```powershell
python -m infra.scripts.verify_deployment
```

Ordinary verification is read-only: it checks ARM, exact role-definition IDs/scopes and
principal, approved PE connections, subnet membership, complete zone groups/VNet links,
single-instance Linux settings, separate SCM, public HTTPS, storage recovery policy,
Kudu, `/health` and `/ready`. It neither imports data nor restarts the app nor launches
a provider. A matching role name or private-endpoint target alone is insufficient.
Kudu reachability is deployment-plane evidence, **not** app-container MI/private DNS proof.

After separate paid-proof authorization, the public proof command is:

```powershell
python -m infra.scripts.verify_deployment `
  --require-proof `
  --benchmark <benchmark-slug> `
  --dataset-id <published-two-pair-synthetic-revision> `
  --proof-state <local-ignored-proof-state-file> `
  --solution <first-solution> --solution <second-solution> `
  --solution <third-solution> --solution <fourth-solution> --solution <fifth-solution>
```

Proof requires the exact ten-task matrix, all five networked solutions, no more than
three total attempts per task, successful evaluated results, request telemetry and
hash-verified manifest/report/task/attempt evidence. For non-public historical
deployments the single-solution proof form remains available. An AZD success message,
readiness response or this provider proof alone does not complete all release gates.
The local proof-state file is created before submission and pins the request and
timestamped idempotency key. Keep/reuse it after a lost response or interrupted
verification; the same proof must not silently create another paid run. A conflicting
selection or expired unaccepted key fails instead of generating new work.

Public upload uses a durable multi-request session: small admission/finalize JSON
(at most 32 KiB), independently idempotent app-proxied raw file PUTs (at most
25,000,000 bytes per audio file and 16,384 per reference), and asynchronous validation
after finalization verifies every reserved part. Admission reserves counts and the
250,000,000-byte aggregate dataset budget. Never send a monolithic 251 MB HTTP
request or expose browser SAS URLs. Deployment does not assume that App Service
accepts a whole dataset in one request.

**Approved provider-dispatch policy: availability with one current-generation
coordinator and bounded concurrent senders.** Opted-in manifests permit at most five
global tasks and one per pack/solution; legacy manifests remain serial. This is still
single-instance execution, not horizontal scaling or a quota guarantee. Admission
stops on leadership/readiness/authorization loss or shutdown, and the process drains
before replacing its executor. Rare duplicate or overlapping external calls may occur after a crash,
a process paused before POST and superseded by takeover, or a lost response. Table
fencing rejects stale canonical/result writes; it cannot guarantee physical
non-overlap or exactly-once provider calls. Persist request-start and indeterminate
usage evidence, retain shared cooldowns, and preserve the three-total-attempt hosted
ceiling. Recovery may continue under the current generation with these explicitly
accepted external-call risks. The proof check counts recorded attempts and does not
certify physical-call non-overlap or erase uncertain usage.

### App-runtime operator operations and complete hosted proof

Do not enable `AI_BENCH_ALLOW_NETWORKED_SOLUTIONS` until actual private Azure Table/Blob
conformance is recorded. `AI_BENCH_EXECUTION_BACKEND=azure-table-blob` is explicit;
there is no `/home` SQLite fallback. The core operator-only `storage conformance` CLI
publishes typed immutable evidence plus a Table marker in the reserved `backend-proofs`
namespace only after the required live scenarios pass. Its JSON result includes
the attestation content digest, `live_promotion` and `provider_calls`. Local/fake
results are not live promotion. An empty or unverified
`AI_BENCH_BACKEND_CONFORMANCE_RECEIPT` is not proof. This preserved environment
identifier carries the attestation digest; its spelling does not replace proof validation.
The active backend validates the marker, content hash, required scenarios, current
supported compatible suite/scenarios and storage configuration, not just the digest shape.
Exact code/dependency matching remains mandatory when publishing and qualifying a build.
No public admin endpoint
or automatically invoked paid postdeploy hook is permitted.
The current core composition selector is `AI_BENCH_STORAGE_BACKEND`; Bicep emits it
and the explicit `AI_BENCH_EXECUTION_BACKEND` declaration consistently. The stable
`AI_BENCH_AZURE_NAMESPACE` defaults to `application-v1` for the new account and is
preserved on omission. Never change the application namespace to run conformance;
the operator tool must use a separate disposable proof namespace. Blob and Table
settings come from ARM primary endpoints (only a trailing Blob slash is normalized).

Use a reviewed Entra-authorized app-runtime SSH/operator path, not Kudu's command
container. `python -m infra.scripts.runtime_probe --worker-pid <actual-api-pid>
--provider-setting AI_BENCH_SPEECH_ENDPOINT --provider-setting AI_BENCH_OPENAI_ENDPOINT
--provider-setting AI_BENCH_MAI_ENDPOINT --output <local-evidence-file>` verifies the
actual API worker process/instance, ManagedIdentityCredential principal, private
Blob/Table DNS addresses, public provider DNS addresses, TLS and Blob/Table read
access without a paid provider request.
The probe refuses a different worker environment or SCM container. Preserve its output
locally; it explicitly does not claim backend conformance.

### Exact operator CLI interfaces

These commands are documented for the separately authorized operator phase; none is
invoked by ordinary validation or the bootstrap hook.

On the source operator machine, with network execution disabled, export the selected
terminal baseline and validate its independently recorded digest:

```powershell
$env:AI_BENCH_RUNTIME = "local"
$env:AI_BENCH_STORAGE_BACKEND = "local"
$env:AI_BENCH_ALLOW_NETWORKED_SOLUTIONS = "false"
python -m ai_bench.cli archive export <baseline-run-id> `
  --state-root <original-local-state> --destination <external-bundle-directory>
python -m ai_bench.cli archive validate <external-bundle-directory> `
  --expected-digest <exported-archive-digest>
python -m ai_bench.cli archive import <external-bundle-directory> `
  --expected-digest <exported-archive-digest> `
  --local-durable --state-root <fresh-local-rehearsal-target>
```

The import is idempotent on identical content, preserves original report/manifest/
failed-attempt bytes, atomically publishes the installed dataset and baseline pins,
and never executes their historical tasks. Rehearse after removing source-directory
availability; do not use filesystem copies of a live SQLite WAL as an export.

Inside the Linux **app runtime**, retain `AI_BENCH_RUNTIME=azure`, MI-only storage
settings and the exact deployed package. Scope the environment override only to
the operator child command, not the App Service settings or running application:

```sh
env AI_BENCH_AZURE_NAMESPACE=conformance-<unique-operation-id> \
  AI_BENCH_ALLOW_NETWORKED_SOLUTIONS=false \
  python -m ai_bench.cli storage conformance \
  --benchmark <installed-synthetic-benchmark> --solution <offline-solution> \
  --state-root <bounded-operator-cache>
```

The namespace must be empty and operation-owned; the CLI rejects a networked solution
or nonsynthetic dataset. This operation writes synthetic data to real Blob/Table, so
it is not an ordinary read-only check. Retain its JSON evidence and the published
digest, and protect the reserved `backend-proofs` namespace from synthetic-job cleanup.
Never promote the app from a local conformance result or a manually composed record.

After that authorized run, set the returned digest in local AZD
`AI_BENCH_BACKEND_CONFORMANCE_RECEIPT`, keep networked solutions disabled, and use the
reviewed deployment path to apply the setting only when the active qualification is
missing/inadequate. A changed configuration or incompatible suite needs new evidence;
significant backend build changes require release conformance without automatically
replacing a compatible runtime qualification. The application namespace stays unchanged.
Then stage the reviewed bundle separately and run, in the app runtime:

```sh
python -m ai_bench.cli archive validate <operator-staged-bundle> \
  --expected-digest <reviewed-archive-digest>
python -m ai_bench.cli archive import <operator-staged-bundle> \
  --expected-digest <reviewed-archive-digest> --state-root <bounded-operator-cache>
```

Do not pass `--local-durable` in Azure. Remove source/staging dependency only after
published hash/closure checks. Generic catalog `landing_context()` selects the
imported baseline; no baseline ID, source path or uploaded data is embedded in the app
package. `/ready` must remain unavailable until conformance and required baseline
publication are complete. Run ordinary `verify_deployment` and the remaining release
proof before enabling explicitly authorized provider work.

Later separately authorized release proof must also:

1. Run shared real Azure lease/ETag/transaction/fencing/recovery conformance in an exact
   disposable namespace, including >100-entity initialization and stale owners. Scope
   cleanup to that approved namespace. Prove unauthorized public Blob access fails.
   Include controlled pause-before-POST/takeover and lost-response regressions:
   demonstrate current-generation progress, stale-write rejection, retained
   indeterminate usage and bounded retry/cooldown behavior, not physical non-overlap.
2. Stage the reviewed archive separately through operator-only SCM/private-host
   transport and import **inside the app runtime**. Validate hashes, the installed
   26-sample revision and original 130-task completed baseline including all attempts.
   Keep imported work non-executable; remove source/staging dependence after publication.
3. Verify the bare public URL opens the pinned baseline; inspect browser console,
   bookmark navigation, audio play/Range/HEAD/download and original report bytes.
4. Upload two locally generated synthetic English audio/reference pairs through the
   public UI, prefer WAV/MP3; launch the ten-task paid proof explicitly. No real upload
   fixture or baseline archive is part of the app package or repository.
5. Restart during recoverable synthetic work and after publication. Verify identical
   durable identities, manifests, evidence, cooldowns, leases, datasets and baseline
   without local files or duplicate archival execution.
6. Exercise ten-day expiry/reaper with an isolated synthetic proof namespace/test clock,
   never age the real baseline. Check API/HEAD/Range denial at expiry, eventual physical
   deletion, shared-hash/pin safety, transient-delete recovery and no stale cache bytes.
7. Repeat preservation/ARM checks; prove public portal/browser/playground network
   reachability for both AI accounts with Entra authorization still required. Verify
   three private storage/vault endpoints and no AI-private resources created by this release.
   Collect the full proof graph/browser evidence before marking the deployment proven.

These live checks remain pending until separate deployment authorization and actual
operator execution. Never use arbitrary self-reported JSON or Kudu
reachability to bypass a failed gate.

## Retention

Uploads and everything derived from them expire ten days after publication; a late
run never extends dataset expiry. New runs on the permanently installed dataset expire
ten days after acceptance. Only the installed revision and designated original completed
baseline are pinned forever. The fenced core reaper, not Blob lifecycle, manages the
reference graph and deletes only unshared/unpinned objects. Access stops at expiry even
if a sweep is delayed. Physical deletion resumes at the next successful sweep after
outages; downloaded copies and provider retention are outside application control.

On the **new application storage account only**, Blob/container soft delete and Blob
versioning are disabled to avoid additional recovery retention. There is no lifecycle
age deletion of CAS evidence. Key Vault soft delete and purge protection remain enabled.
Monitoring must not capture uploads, filenames, references or content-bearing artifact
URLs; platform diagnostics exclude HTTP/console content logs.

## Redeployment

### Code-bound parallel-execution cutover

Execution, repository, pack-loading and concurrency-conformance sources are included in
the backend signature. A significant backend change requires exact-build release
qualification; compatible runtime readiness may retain its genuine prior attestation.
Prepare the exact frozen candidate in a separate app-runtime operator directory without
starting its ordinary server/supervisor. Run its synthetic conformance only after the
operator has approved the exact write/delete scope and an idle cutover window.
The child process alone selects its new, empty `conformance-<operation>` namespace and
disables networked solutions. Never change the application's namespace or network flag.
Record the candidate package digest, actual Python minor and signature-bound dependency
versions from that process; the installed candidate must use those same versions.

Conformance is not read-only and is not optional cleanup. The existing suite:

- mutates synthetic coordination rows only in its operation-owned Table partition;
  task, lease, due/event, descriptor and lifecycle rows are conditionally updated/deleted;
- overwrites one owned synthetic Blob to exercise integrity, restores it, then attempts
  a stale-ETag delete that must fail without removing the replacement;
- advances only its in-process lifecycle clock, expires the owned synthetic runs and
  invokes real fenced reaping, deleting their unshared objects, descriptors, receipts,
  operational rows and local report/spool files;
- reimports the owned synthetic terminal archive as a pin, expires a new derived run,
  and proves shared pinned evidence survives;
- publishes the genuine attestation and marker under the separate `backend-proofs`
  namespace only after every required scenario passes.

Blob operations are confined to `namespaces/<operation-namespace>/sha256/`,
`descriptors/` and `receipts/` in the configured private containers. Staging projections
are isolated by their prefix and the same operation namespace. The proof's objects
and Table marker use `backend-proofs`, not the application or synthetic partition,
and are not reaped by the synthetic operation. Local cleanup targets are under the
explicit operator state/cache directory. No account/container/Table deletion or
application data mutation is authorized by this procedure. If approval covers checks
but not these destructive test cases, stop for that exact scope; do not omit cases,
forge an attestation or weaken readiness. Leave remaining proof data intact unless cleanup
is separately approved.

Before cutover, retain the exact old ZIP plus attestation, Python minor and dependency
versions as one rollback pair. The pre-policy package's closed manifest schema cannot
read the new `scheduling_policy` field. That old pair is usable only before any
new-policy manifest has been accepted, not merely when no run is currently active.
If a candidate launch has occurred, stop for a reviewed reader-compatible forward fix
or rollback artifact and matching proof; never strip the field, rewrite a manifest,
or silently restore a package that cannot read the new evidence.
Verify no executable run is active immediately before
any setting/package change; if public launches prevent an idle window, stop for an
explicit maintenance decision, never cancel runs or silently disable admission.
When runtime qualification is inadequate, the ordering is: publish candidate conformance,
change only `AI_BENCH_BACKEND_CONFORMANCE_RECEIPT` to that digest, then install the ZIP.
Otherwise retain the compatible setting and install the qualified package directly.
The old package/new attestation interval intentionally fails readiness; setting changes
can restart the app, so do not describe it as uninterrupted. Verify Kudu completion,
actual serving source hashes/dependencies, attestation, health/readiness and unchanged
historical evidence after the package lands. Rollback restores the retained old attestation
setting followed by its exact old ZIP/dependencies only within that reader-compatible
window, with the same brief readiness
mismatch; never revert stored evidence. Synthetic concurrency proof is not observation
of production provider concurrency and authorizes no paid proof call.

- Omitted optional parameters preserve existing configuration.
- Existing resource groups are scoped, not redeclared, so their tags remain intact.
- Existing Foundry accounts are referenced, not recreated.
- Role assignments use deterministic IDs.
- Mode switching uses the explicit secret synchronization workflow.
- Deployment scripts are idempotent and bounded.
- The baseline is single-instance. Do not scale App Service beyond one instance until a
  shared coordination repository passes all lease, retry, reconciliation, and finalizer
  fencing tests.

## Failure handling

- Authentication failure: inspect identity, account scope, role assignment, and RBAC
  propagation before changing credentials.
- Private endpoint failure: inspect subnet policy, private DNS links, and App Service
  VNet integration.
- Readiness failure: inspect storage data-plane roles, DNS, and configuration.
- Kudu failure: verify SCM restrictions separately from the main site.
- Ingress failure: inspect main and SCM rules independently. The public demo main site
  is unrestricted; SCM must match its explicitly selected bounded-CIDR or Entra-only
  public-demo policy and retain Entra permissions/disabled basic publishing.
- AZD status lag: query ARM and endpoints directly; do not repeat deployment blindly.

## Cleanup

Cleanup is destructive. Show the exact environment and resources, preserve any reused
resource group or Foundry account, and obtain explicit user confirmation before running
`azd down` or any delete command. Never purge Key Vault as routine cleanup.
