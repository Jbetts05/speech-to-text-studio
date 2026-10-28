# Benchmark Contract

## Contract goals

The platform core coordinates immutable data, execution, evidence, and reports. A
benchmark pack supplies the task-specific meaning. Packs must be portable, validated,
content-addressed, and discoverable without changing a core registry.

## Benchmark definition and terminology

`BenchmarkDefinition` declares:

- stable ID, display name, semantic version, description, and tags;
- `Terminology`, which maps benchmark, sample, solution, and reference to pack-facing
  labels;
- accepted input and reference modalities;

The pack composition binds that definition to its dataset adapter, solutions, evaluator,
artifact viewer, pricing snapshots, compatibility declarations, and explicit standalone
or assisted pipelines.

Terminology affects presentation only. Storage paths, API fields, task identities, and
platform behavior keep generic names.

## Dataset import and validation

A valid pack catalog is separate from external dataset availability. External adapters
implement `availability()` with typed `ready`, `not-configured`, or `invalid` states
and `snapshot_metadata()`. Discovery validates their catalog contracts without reading
data, resolving credentials, or constructing clients. Availability includes planned
sample counts and an actionable explanation; `ready` requires a complete revision.
There is no empty-dataset substitute. Launch calls full dataset validation before
publishing executable tasks.

A dataset adapter converts a source package into one immutable dataset revision. The
importer must validate before publication:

- package manifest version and benchmark ID;
- path traversal, absolute paths, symlinks, duplicate and case-colliding names;
- file count, per-file size, total expanded size, compression ratio, and encryption;
- declared media type, decodability, dimensions/duration, and schema;
- case IDs, roles (`scored`, `few-shot`, `excluded`), and reference requirements;
- separate total and scored sample counts so task estimates exclude few-shot and
  excluded examples;
- artifact hashes and source provenance;
- license/provenance metadata supplied by the adopter.

`samples()` returns immutable artifact descriptors; `read_artifact()` returns the exact
bytes for every input and reference modality. The platform verifies each byte length and
SHA-256 identity and stores every artifact before exposing executable tasks. Idempotent
launch replay repairs interrupted initialization before returning the existing run.

Version 2 run initialization also persists a content-addressed `SampleSnapshot`,
including all sample metadata and pack-owned publication provenance. Execution reads
this snapshot and stored input bytes. `ExecutionRequest` contains input bytes and an
input-only sample: references are removed before the adapter is invoked. References
remain available exclusively to the evaluator and evidence viewers.

### Optional scheduling policy

Packs may declare an inert typed `SchedulingPolicy` with version
`bounded-solutions-v1`, `max_in_flight=5` and `max_per_solution=1`. The platform owns
admission and enforces these ceilings across runs under one fenced supervisor.
The lane identity is the tuple `(benchmark.id, solution.id)`, not a concatenated
string, provider label or throttle scope. Distinct solutions can share a cooldown.
Absent policies mean serial execution; unsupported/untyped policies fail validation.

New runs pin the policy and its conditional `scheduling-policy` descriptor hash.
Absent historical policies add neither a serialized field nor a descriptor hash.
Existing idempotency reservations still compare the exact original request fingerprint
and replay their original manifest, even after a pack opts in. A changed request still
conflicts. No historical manifest, report, run ID or recorded metric is rewritten.

Publication is all-or-nothing from the task scheduler's perspective. A revision has an
archive hash and semantic content hash. Published revisions are frozen. Updating data
creates a new revision.

### Optional public import capability

An installed pack may expose a versioned dataset importer and a typed, inert
description of its accepted files and limits. Packs without this extension keep their
existing dataset adapter. Discovery must not acquire data, construct provider clients,
decode files or request credentials merely to inspect an importer.

The platform owns HTTP transport, admission, durable upload sessions, immutable object
storage, catalog publication, retention and idempotency. The pack owns its file roles,
pairing, content/media/schema validation, sample construction and provenance. No
platform branch may recognize a pack by name.

Public upload is a multi-request state machine, not a large multipart dataset POST.
Small admission JSON reserves all names, exact sizes, SHA-256 identities, acknowledgments
and aggregate capacity. Each independently idempotent raw file request is bounded by
its reservation before parsing or storage. File completion is acknowledged only after
its bytes are durable and hash-valid. Status supports interruption/resume. Finalize
checks every exact reserved part and total before scheduling asynchronous validation.
Only a complete validated revision is atomically exposed to discovery and launches.
Incomplete sessions expire and are reaped; abandoned parts never become public artifacts.

A catalog selection is distinct from the pack's default dataset. Launch fingerprints
include the selected catalog identity, while manifests pin the immutable revision,
ordered sample descriptors and stored snapshot. Existing launch replay repairs the
same reservation rather than resolving whichever dataset is current now.

### Retention and archival roots

Operational ownership, expiry, pinning and archive status are separate records, not
edits to immutable manifests or reports. A dataset's fixed expiry bounds every derived
run, attempt, report and artifact. Reads, retries, exports and late launches cannot
extend the root deadline. A permanently installed dataset does not automatically pin
every run created from it.

Public reads, including artifact ranges, HEAD and conditional responses, enforce
expiry independently of physical cleanup. A fenced, resumable reaper removes catalog,
history and evidence indexes, operational rows, ownership references, unreferenced
objects and local temporary data. Conditional deletion generations protect shared
content and explicitly pinned archival roots from stale cleanup workers.

Archive transfer preserves original manifest/report/object bytes, descriptor
occurrences and the full terminal evidence graph. Export is consistent, independently
hash-verifiable, and includes the pack-owned publication closure. Import stages
invisibly and publishes atomically, is idempotent on identical content, and rejects
conflicting identities. Imported terminal runs are explicitly non-executable and
cannot be replayed through the worker or an idempotent launch.

## Solution adapters

`SolutionAdapter` separates:

1. `execute`: invoke an offline implementation or explicitly configured provider and
   return raw evidence, usage, and an optional provider request identifier.
2. `parse`: convert raw evidence into the pack's declared output contract.

Adapters declare:

- stable adapter ID and version;
- whether execution is offline or networked;
- pipeline type and every component;
- compatibility and quality-comparability state;
- a regional, versioned pricing snapshot;
- raw modality and media type, parsed output, usage dimensions, and an optional safe provider
  request ID when execution completes.

The platform `config.json` schema contains only discovery and network-safety flags.
Pack-specific limits, timeouts, concurrency, and provider settings belong in typed,
versioned adapter code or a separately validated pack contract; changing comparable
behavior requires a new adapter version. Networked adapters never activate from ambient
credentials alone.

### Optional service display name

An adapter may expose an inert `display_name` string for catalog presentation. It
must not read provider configuration, credentials or the network. The public catalog
projects this optional label separately from the stable solution ID. Absent, blank
or non-string metadata has no display label; clients retain the exact ID as fallback.
Pack-owned labels may include a model version when needed to distinguish services.
They do not change identity, eligibility, pipeline settings, manifests or pricing.

Public-demo primary navigation and service selection use the explicit runtime landing
benchmark as presentation scope. This is not an authorization boundary: discovery
and saved-run evidence remain unchanged. Missing or unknown runtime/landing context
shows an actionable unavailable state rather than falling back to a developer pack.
Recognized non-public local and Azure runtimes keep the installed catalog and saved-run
history without requiring a landing configuration. Saved runs outside the public
scope remain readable but do not supply new-run service choices.

## Standalone and assisted pipelines

A standalone pipeline has one result-producing component. An assisted pipeline declares
all preprocessing, helper, model, postprocessing, and normalization components. Each
adapter must preserve the component provenance needed to interpret its raw evidence and
usage. Component-specific metrics may be added without combining the independent metric
families.

The same provider may appear in either pipeline type; names do not determine type.

## Evaluators and metric definitions

An evaluator receives the current sample (including its optional reference) and parsed
output. It returns comparison evidence and metric observations, then aggregates typed
evaluation records for a single solution.

Each `MetricDefinition` declares:

- ID, display name, description, family, unit, and whether higher or lower values are
  preferable;
- an evaluator-owned aggregation implementation;
- explicit unknown values with a reason in each `MetricValue`.

Metric families are `quality`, `cost`, `latency`, `usage`, and `reliability`. Reports may
group them but may not combine them into a universal score.

## Artifact viewers

An `ArtifactViewer` declares its modality and media types, renderer ID and version,
accessible title, and safe download fallback. Core contracts cover image, document,
audio, video, text, and structured artifacts. A pack can add a renderer descriptor
without changing evidence storage.

Viewer code treats artifact content as untrusted. It must not execute active content,
leak signed URLs, or depend on a local absolute path.

### UI extension boundary

The Evidence Ledger UI is platform-owned. Packs configure only these slots:

- benchmark terminology and descriptive metadata;
- declared input and reference modalities;
- solution and pipeline descriptors;
- metric definitions, applicability, units, and comparability;
- the artifact viewer descriptor and safe modality renderer;
- immutable evidence records and download links.

Packs do not own the application shell, navigation grouping, context rail, evaluation
toolbar, five-family summary, solution-performance table, comparison geometry,
operational-detail placement, typography, colors, spacing, or responsive breakpoints.
Those remain unchanged for every generated repository unless the adopter explicitly
requests a platform redesign. In particular, a pack must not replace the main
result-comparison view with raw provider payloads. Result-level comparison evidence is
primary; raw, parsed, provider, and attempt evidence remains available in the
operational artifact view.

## Pricing and usage

A `PricingModel` is the pinned pricing snapshot. It declares an ID, version, region,
currency, effective time, source, and dimension-based rates.

- Retry usage remains in total cost evidence.
- Every priced usage dimension has an explicit unit size and rate.
- A missing rate produces unknown cost, not zero.
- Zero is valid only when an explicit snapshot says the component is free/offline.
- Historical runs retain their pinned snapshot even after rates change.

### Optional read-only current pricing

A pack may expose an inert `pricing_provider` implementing
`CurrentPricingProvider.quote(SolutionDescriptor) -> PricingQuote`. The descriptor is
the immutable run's pinned configuration, not the currently installed adapter settings.
The pack owns exact model/version/region/meter applicability, reviewed source URLs,
effective and checked dates, unit rates, explicit assumptions and unavailable reasons.
No provider call, credential access or live pricing lookup occurs while viewing costs.
An absent extension means no current quote, not a free estimate or a broken offline pack.

The additive run-detail `costs` projection is separate from the canonical report.
Recorded costs use original attempt telemetry, including failed and retried attempts;
terminal report attempt history takes precedence when available. Per-solution, sample
and run views retain currency-specific subtotals, covered/expected attempts, terminal
task coverage and missing-evidence reasons. Currencies are never added together.
No attempts, missing history or unobserved usage cannot establish zero or complete cost.

A current-rate estimate prices only the quote's declared recorded usage dimensions.
It can estimate submitted work even when provider billing is unknown, but must remain
labelled conditional and not billed cost. Missing required dimensions make coverage
partial/unknown. A quote without rates is unknown. Assumptions such as an unpinned
deployment tier, pro-rata rather than billing rounding, and unconfirmed billability
must be visible with provenance. Never substitute another model's price.
The projection neither rewrites manifests, attempt evidence, results, JSON reports nor
CSV exports, and its checked date can differ from the original run's date.

## Compatibility

Compatibility is data:

- `full`: the solution can produce the complete contract.
- `partial-reference`: the solution is useful but cannot produce every reference field
  or event.
- `unsupported`: the solution cannot run the case or benchmark.

`comparable_quality` is separate from compatibility state. Mappings and normalizers are
identified and versioned. Unsupported solutions cannot be selected for execution.
Solutions with `comparable_quality=false` may still produce cost, latency, usage, and
reliability evidence, but their report quality aggregates remain unknown and retain the
pinned compatibility reason.

## Immutable run manifests

A run manifest pins:

- benchmark definition and version;
- dataset revision and ordered case hashes;
- solution and pipeline configurations;
- solution-adapter, evaluator, viewer, compatibility, and pricing versions;
- retry/network limits and content hashes for every pinned descriptor;
- manifest schema version and creation time.

The manifest hash is the run's reproducibility anchor. It is written once and never
updated. Before task execution and finalization, the platform recomputes the benchmark,
dataset, solution, evaluator, viewer, compatibility, and pricing descriptor hashes.
Resume fails closed on implementation/settings drift rather than mixing versions in
one run. Version 2 dataset identity is checked against the persisted snapshot, not a
mutable acquisition directory; an unavailable current dataset cannot invalidate stored
run inputs. Version 1 manifests remain readable and retain their legacy descriptor
hashing and dataset drift checks. New optional model fields are excluded when
recomputing legacy descriptor hashes.

Configured solutions expose validated non-secret `execution_settings()` and an opaque
`throttle_scope`. The manifest hashes behavioral selectors, request options, endpoint
identities, retry policy, and normalization alongside existing descriptors. These pins
do not claim that a provider's backend weights are immutable.

## Asynchronous launch

`POST /api/runs` returns 202 only after manifest, snapshot, input/reference bytes and
recoverable tasks are persisted. A lifespan-managed single coordinator owns a renewable
backend supervisor lease. Launch-intent publication retries bounded root-only CAS
contention under a fresh live-root check; an identical intent is idempotent, a changed
intent remains a conflict, and expiry or exhausted contention fails explicitly.
The coordinator repairs interrupted initialization, recovers expired task
leases and observes runtime network authorization. Request-scoped background tasks are
not the persistence mechanism. Work waits until due time or a wakeup, with bounded
rechecks for recovery. Local execution uses SQLite/filesystem. The hosted Table/Blob
backend must pass the same complete protocol/fencing conformance before promotion;
provisioning storage or passing a readiness check alone is not promotion.

The synchronous driver also owns a renewable supervisor generation through execution
and report finalization. Renewal never reacquires an expired or replaced generation;
renewal loss closes admission, and cleanup can release only its captured generation,
even if another invocation reused the owner string. Terminal report replay is read-only.

Hosted readiness validates a genuine published qualification for the supported
versioned backend suite, complete required scenarios, backend/identity and unchanged
configuration. It does not require source/dependency hashes to equal the current
build. Those hashes are retained and enforced when publishing exact-build proof and
by the release workflow for significant backend changes. Legacy 17-case proofs cannot
satisfy the current 21-case contract; there is no implicit legacy promotion.
Breaking storage/scheduler/fencing/recovery compatibility requires a reviewed suite
version change and new qualification; unrelated UI/app releases reuse compatible proof.

The hosted coordinator uses conditional entity versions and atomic state transitions.
Initialization larger than a service transaction limit is staged in bounded batches;
the complete expected matrix and immutable closure must exist before tasks become
claimable. Supervisor, task, finalizer, retention and object-deletion generations
prevent stale writers from publishing canonical state.

Attempt persistence retains an operation-local parsed value only while its immutable
artifact ID is unchanged. After Blob/ownership writes it refreshes task/attempt rows
and live root/supervisor fences at the commit boundary. A changed attempt pointer
restarts the transform; takeover/expiry still rejects the write. All ETag conditions
and bounded contention retries remain, without serializing provider calls.
Within one lifecycle instance, ownership registration serializes only its state
GET/CAS critical section to avoid competing allocations of shared generation rows.
Blob work and provider calls remain outside that lock; external writers/deletion
still require the same distributed ETag, generation and expiry checks.

The owner thread alone selects eligible run/solution lanes and claims tasks, with a
free executor slot before leasing. SQLite and Table selection scan beyond a blocked
first page; ineligible waiting tasks consume no attempt. Legacy serial work does not
overlap other lanes. New opted-in work uses at most five concurrent tasks globally and
one per solution identity, without a per-file barrier. Pack loading is serialized;
the owner prepares private pack/adapter/evaluator instances before submission. Provider
calls hold no orchestration lock. Completed callbacks only wake the owner.

Admission closes on shutdown, lost leadership/readiness, expiry or revoked provider
authorization. The same process drains its executor before releasing leadership or
starting another pool; cancelling a future is not HTTP cancellation. Physical retention
maintenance waits for active work to drain; public expiry checks remain immediate.
Upload processing and initialization recovery continue on the owner, while lease
recovery explicitly excludes its in-flight task IDs. A run cannot finalize while its
executor tasks remain active. Durable response checkpoints survive storage failures
without a resend; unresolved attempts retain their honest recovery state.

The selected availability policy has one current-generation coordinator with bounded
concurrent senders. Generation fencing
does not promise physical provider-call non-overlap or exactly-once billing: a paused
sender can issue its old call after takeover, and a lost response can lead to a
duplicate. Persist request-start and indeterminate usage evidence, fence stale
canonical writes, retain response checkpoints and enforce attempt ceilings/cooldowns.
Do not automatically repeat an invocation merely because storing its observed response
failed. Pause-before-POST takeover and lost-response regressions must demonstrate this
bounded duplicate-risk policy instead of asserting external exclusivity.

Before physical dispatch, `attempt.request_started` and the attempt's request-start
checkpoint commit atomically. The event records the task, attempt, lease token and
supervisor generation with `submission=unconfirmed`; it does not assert that the
provider received or billed the request. A failed transaction leaves neither record,
and a repeated request-start attempt cannot append another event or resend.

Network authorization gates provider attempts, not provider-free finalization of fully
terminal runs. Finalization clears stale runtime-blocked state. An interrupted version
1 launch reservation without a manifest is recognized by its legacy identity and
revalidated against the original descriptor fingerprint before upgrading its request
fingerprint. Its reserved run ID is preserved, and no existing manifest is rewritten.

For a known `azure` runtime with `allow_networked=true`, the hosted Launch action
explicitly supplies `allow_networked=true` in the request. A passive paid-call/retry
note replaces the redundant hosted checkbox. Local runtimes retain the checkbox;
unknown runtime modes never gain consent. Server runtime authorization, readiness,
compatibility, idempotency, attempt ceilings, cooldowns and admission remain mandatory.

Verified Blob reads retain a hard bound of four open readers. A read waits at most one
second for a released permit; saturation then produces HTTP 503 with Retry-After.
Closing a reader or failing verification releases its permit and removes its spool.
Byte-only content-addressed reads verify a fresh bounded Blob length, ETag and SHA-256
without enumerating presentation descriptors. Metadata-aware retrieval and streams
still select the canonical/media descriptor and verify its registered length.
Caller ownership, publication and expiry checks remain unchanged.

## Tasks, attempts, telemetry, and artifacts

The stable task identity is derived from run, case, and solution. Every execution
attempt has:

- attempt number, lease-fencing token, and status;
- start/finish timestamps and end-to-end duration, including failed attempts;
- provider latency, usage, and cost when provider execution completed;
- raw and parsed artifacts retained before later processing fails;
- parsed artifact or parse error;
- an optional provider request identifier when safe to retain;
- typed error classification/message and terminal classification.

Canonical comparison and result artifacts are materialized only after attempt evidence.
Every evidence link carries the stored artifact's actual modality, media type, and byte
size; viewers do not infer those values from a pack-wide default. Events are append-only
and terminal events are idempotent. Artifact identity is the content SHA-256, while each
evidence occurrence retains its own descriptor. If identical bytes validly represent
multiple media types, the artifact endpoint honors the evidence viewer's exact
`Accept` media type; a generic download uses the deterministic canonical descriptor.

Attempt evidence validation verifies a matching registered descriptor by its exact
content hash and artifact identity. It does not select or download every presentation
variant of the same bytes. Registration lookup remains paged; missing, corrupt or
inconsistent descriptors fail before evidence is recorded. Canonical download
selection, immutable descriptor occurrences and expiry/lease fences are unchanged.

`ProviderFailureEvidence` carries a safe classification, HTTP status, request ID,
retryability, known usage and uncertainty, raw artifact identity, redaction/truncation
state, and provider/scheduled delay with source. Raw bytes precede the failure record;
failure and cooldown deadline are committed together before retry scheduling. Restart
cannot erase a provider delay, including a crash between failure recording and retry.
Cooldowns are scoped by opaque provider/deployment identity. Waiting consumes no attempt
and holds no task lease; independent scopes remain eligible.

`Usage.complete=false` requires an uncertainty reason and prevents a zero-cost
inference. Reports separate provider latency, full attempt duration, queue wait,
cooldown wait and run elapsed time. Expected/evaluated sample IDs and completeness are
reported separately from declared capability. Partial or different coverage is not a
complete comparable quality result.

## Report evidence

Aggregate observations link to contributing sample comparisons. A report includes its
manifest reference, terminal status, per-task attempt evidence, independent metric
families, reliability outcomes, and evidence links. JSON is canonical; CSV is a derived
view.

### Finalized evaluation reads

`GET /api/runs/{run_id}/finalized-evaluation` returns a typed
`record_source: "finalized_report"` envelope containing current public run metadata,
the hash-verified immutable manifest and report, sample evidence descriptors, and the
separate cost projection. `report.results` is the historical result matrix, not a
projection of current task scheduler state. It does not invent leases, cooldowns or
live task records, nor read individual task, result, attempt or event rows.

The endpoint verifies terminal run/report identity, manifest agreement, exact
sample-by-solution task identities, terminal outcomes, contiguous complete recorded
attempt history and pinned pricing identity before aggregating retry-inclusive costs.
Missing telemetry or unknown pricing remains explicit even when history is complete.
Existing run, dataset, artifact ownership and expiry gates apply, including a final
lifecycle check. Responses are not cacheable public snapshots.

Nonterminal, missing-report and incomplete historical records explicitly return
`record_source: "live_detail_required"` with a typed reason. Clients then use the
existing live detail endpoint. Integrity mismatches are HTTP 409, not a fallback;
unavailable ownership and expiry remain errors. Original JSON/CSV/report/result bytes
are unchanged. Live diagnostics continue to use the existing operational endpoints.

## Configuration and versioning

- Unknown configuration keys fail validation.
- IDs use lowercase stable slugs.
- Contract-breaking changes increment the benchmark contract major version.
- Evaluator, pricing, and viewer changes are independently versioned.
- A pack cannot override platform safety limits.

Public API envelopes are typed backend contracts. `scripts/generate_api_types.py`
generates the frontend wire aliases from serialization schemas, including historical
manifest versions and the declared transcript-renderer boundary. The Python suite
rejects generated-type drift; UI fixtures compile against these types. Edit source
contracts and regenerate rather than hand-editing generated types or frontend assets.
