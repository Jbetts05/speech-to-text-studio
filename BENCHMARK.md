# Speech-to-Text Studio

## Identity

- **Name:** Speech-to-Text Studio
- **Slug:** `speech_to_text`
- **Purpose:** Compare five managed transcription solutions on the preserved,
  duration-stratified 26-sample LibriSpeech test-clean pilot or an explicitly selected
  immutable English upload.
- **Primary users:** Engineers and reviewers inspecting transcription evidence.

## Decision this benchmark supports

Inspect transcription errors, operational reliability, and independent resource
tradeoffs on the same explicitly selected dataset. The installed pilot is not the full
test-clean benchmark, a representative production workload, a statistical ranking, or
a universal recommendation. Uploaded datasets are user-supplied, not validated
population samples. Different datasets or evaluated-case coverage are not a complete
comparison.

## Modality

- **Input modality:** Audio.
- **Reference modality:** Text.
- **Accepted media/types:** The installed pilot retains original `audio/flac`, 16 kHz
  mono 16-bit. Public uploads accept validated original FLAC, WAV or MP3 and UTF-8 TXT.
- **Artifact viewer needs:** Native audio controls, accessible normalized transcript
  alignment, original reference and hypothesis, and safe downloads.
- **Visual override:** None; inherit the platform-owned Evidence Ledger unchanged.

## Dataset

- **Source:** [OpenSLR SLR12](https://www.openslr.org/12), LibriSpeech test-clean.
- **License/provenance:** CC BY 4.0, as declared by the official source. Attribute Vassil
  Panayotov, Guoguo Chen, Daniel Povey, and Sanjeev Khudanpur, *LibriSpeech: an ASR corpus
  based on public domain audio books*, ICASSP 2015. Retain upstream attribution in the
  external publication. No source audio, references, archive, selection, or results are
  distributed in this repository or application package.
- **Import format:** Explicit pack-local acquisition of the official `test-clean.tar.gz`;
  verify the upstream MD5 `32fa31d27d2e1cad72775fee3f4849a9` against the
  [official checksum list](https://www.openslr.org/resources/12/md5sum.txt), also record
  SHA-256, validate all 2,620 paired utterances, then atomically publish externally.
- **Scored sample rule:** Exactly 26 unique utterances selected by
  `duration-strata-speaker-chapter-v1`, seed `speech-to-text-pilot-v1`. Sort exact
  frame-count/sample-rate durations with utterance-ID ties. Divide ranks into 26
  contiguous strata using boundaries `floor(i * 2620 / 26)`. Preselect shortest rank 0,
  lower median rank 1309, and longest rank 2619. Anchors fill their strata. Visit other
  strata in ascending order and choose the least represented speaker, then chapter,
  then the lowest SHA-256 of canonical seed/release/utterance-ID data. Return selection
  in stratum order. Reference content and provider outcomes never influence selection.
- **Excluded/few-shot rule:** All other utterances remain outside execution. There are
  no few-shot samples, prompting examples, or live microphone inputs.
- **Validation limits:** Exactly 2,620 paired utterances; at most 4,000 archive members,
  16 MiB per member, 1 GiB expanded, 512 MiB compressed, and expansion ratio at most
  100. Reject unsafe names, links, special files, duplicate/case-colliding paths,
  mismatched references, corrupt FLAC, incorrect metadata, and incomplete publication.

### Public uploads

Uploads are English-only and use the same pinned normalization/WER contract. Accept
1-10 exact basename pairs, each containing one `.flac`, `.wav` or `.mp3` and one `.txt`.
Matching is case-sensitive; reject duplicate, Unicode-normalized and case-colliding
names rather than guessing a pairing. No archives, paths, links, external media URLs,
playlists or active documents are accepted.

Limits are 25,000,000 bytes per audio file, 16,384 bytes per strict UTF-8 reference,
250,000,000 aggregate file bytes, 300 seconds per audio and 1,800 seconds per dataset.
Validate actual media/container, full bounded decode, positive duration, supported
mono/stereo sample metadata and a nonempty normalized reference. Original bytes are
never converted, trimmed or resampled. Keep the existing bounded evaluation work limit.
The installed 26-sample revision keeps its original, separate validation contract.

The browser selects files directly but sends a durable multi-request upload session:
small admission JSON reserves every filename, byte count and SHA-256; independently
idempotent raw file requests persist the reserved parts; finalize verifies exact parts
and totals before asynchronous validation and atomic publication. Admission/finalize
JSON is limited to 32 KiB, and no request contains the entire dataset. Failed,
interrupted or recycled reception is resumable per file and cannot expose a partial
revision. Incomplete sessions expire and are reaped.

Before transmission, the uploader must acknowledge public visibility, rights to share,
the English-only evaluation contract and expiry. There is no ownership or private-user
area: anyone can view, play or download published data. Rights and language are
uploader declarations, not a claim of automatic legal or language certification.

Uploaded datasets and everything derived from them expire **ten days after publication**.
Starting another run, viewing, retrying or exporting does not extend that deadline.
Only the installed LibriSpeech revision and designated original completed pilot are
permanently pinned. New ordinary runs on the installed revision expire after ten days.
All retrieval paths enforce expiry; a resumable reaper removes indexes, references,
unshared objects and local spools without deleting another live root or a baseline pin.
There is no additional Blob soft-delete/version-history recovery window for uploads.
Downloaded copies and provider-side retention cannot be recalled.

## Solutions

| Solution ID | Pipeline type | Components | Authentication | Compatibility |
|---|---|---|---|---|
| `azure-speech-fast` | standalone | Azure Speech standard fast transcription | Entra only | full |
| `gpt-transcribe` | standalone | Managed OpenAI Audio deployment | Entra only | full |
| `managed-whisper` | standalone | Managed OpenAI Audio deployment | Entra only | full |
| `mai-transcribe-1-5` | standalone | Azure Speech fast with `MAI-Transcribe-1.5` | Entra only | full |
| `mai-transcribe-2` | standalone | Azure Speech fast with `MAI-Transcribe-2` | Entra only | full |

Exactly five solutions; none is an offline substitute. Clean-checkout platform testing
uses the separate synthetic pack. Execution uploads the validated original audio bytes
with their actual media type and a safe opaque filename, never references, prompts or
reference metadata. The installed pilot remains unchanged FLAC. Parsing is a separate
operation and yields one typed transcript.

Speech fast pins API `2025-10-15`; standard locale is `en-US`. MAI uses locale `en` and
`enhancedMode.enabled=true` with the exact model selector. Managed OpenAI Audio pins API
`2025-03-01-preview`, language `en`, and explicit deployment names. Deployment model pins
are `gpt-transcribe` version `2026-07-28` and `whisper` version `001`. All endpoints and
deployment names are explicitly configured externally, never derived from names or
regions. Region metadata is Sweden Central for standard/OpenAI and East US for MAI.
These selectors do not imply immutable provider backend weights. Do not display
service lifecycle labels in the UI.

## Evaluation

### Quality metrics

Version `librispeech-english-v1` normalizes case, Unicode compatibility forms, apostrophes,
punctuation, and whitespace. Preserve internal apostrophes and contractions; numerals
remain literal rather than guessing spoken expansions. Pin this policy in each run.
Deterministic Levenshtein alignment prefers match, substitution, deletion, then insertion
when equal-cost paths exist. Preserve reference/hypothesis tokens, alignment, substitution,
deletion, insertion, reference-word counts, and per-sample WER.

Corpus WER is `sum(S + D + I) / sum(reference words)`, not mean sentence WER and not
clamped at 100%. Successful empty transcripts count as deletions; provider failures
have no hypothesis and are excluded from evaluated quality, with explicit coverage.

### Cost

Original versioned unknown-price snapshots retain their regional identity and reason;
the pilot's immutable recorded costs remain unknown. Viewing newer pricing must never
rewrite these snapshots or reports.

Separate current-rate estimates use exact public USD/hour meters reviewed on 2026-09-19:
Sweden Central GPT-Transcribe Global 0.27, regional managed Whisper 0.4356, and standard
Fast Transcription 0.36 in Sweden Central/East US. Microsoft's Speech pricing footnote 9
explicitly maps LLM Speech/MAI to the Fast SKU; this is not a borrowed model price.
The pack matches pinned model/version/region and discloses deployment-tier assumptions.
See [pricing provenance and limits](docs/speech-to-text.md#current-rate-cost-estimates).
Estimates are conditional pro-rata recorded audio usage, not original or billed costs.
Retain retry usage, unknown/missing attempt coverage, real zero and partial subtotals.

### Latency

Keep provider-request latency, complete attempt duration, queue/cooldown wait, and
end-to-end elapsed time distinct. Include failed attempts and waiting in their correct
families; do not label wall-clock time as provider latency.

### Usage

Retain known physical calls, submitted audio seconds, and provider-reported usage.
Explicit uncertainty accompanies incomplete billing/usage information, including 429s.

### Reliability

Expose successful/evaluated coverage, failed tasks, attempt counts, retryability, safe
failure classifications, HTTP status, request identifiers, and retry-delay sources.
Provider cooldowns are persisted by opaque account/deployment throttle scope; one
throttled scope does not prevent unrelated scopes from progressing.

## Compatibility

All five APIs accepted original FLAC in the separate compatibility probe. The pilot
supports the full transcript contract. Keep coverage separate from declared capability:
partial results are labelled partial, and different sample sets are not presented as
complete comparable results. Quality, cost, latency, usage, and reliability stay separate.

## Evidence and reports

Persist the immutable manifest, selected sample snapshot, original input/reference
bytes, upstream identity, inventory hash, deterministic selection and coverage,
behavioral settings, raw successful/failed responses, parsed transcripts, normalized
alignment, attempt telemetry, and canonical JSON/derived CSV reports. Use
content-addressed evidence, not mutable acquisition paths. Canonical result comparison
is the primary review surface; provider and attempt artifacts stay in operational detail.

## Runtime and durability

The full pilot contains 26 samples and 130 tasks. Network execution is disabled by
default and requires both run authorization and current runtime authorization.
`POST /api/runs` acknowledges with 202 only after durable initialization. A
lifecycle-managed single coordinator recovers work on startup, uses leases, heartbeats and
finalizer fencing, and waits for due work without busy polling. Local coordination uses
SQLite; hosted coordination requires the conformant Azure Table backend and private
Blob evidence. Idempotency replays repair initialization without duplicating work.

The public hosted Launch action supplies explicit per-run network authorization when
the known hosted runtime permits it, with a passive paid-usage note and no extra
network checkbox. Local operation retains explicit opt-in. Operator-controlled runtime
enablement is always required. There is no daily/monthly spend cap or
cumulative launch limit. Bounded admission and temporary 429/Retry-After backpressure
protect the service without inventing a free-provider substitute.

New speech runs pin the typed `bounded-solutions-v1` policy: at most five active
file/solution tasks globally across runs, and one per pack/solution identity. Independent
lanes advance without waiting for all solutions on a file. Historical manifests without
this policy and other packs remain serial. A waiting task is never leased merely to
reserve a lane. Retry/cooldown waits hold no executor slot. MAI versions retain their
shared provider throttle scope; concurrency does not guarantee provider quota.

Availability uses one current-generation coordinator with bounded concurrent senders.
It stops admission on stop, leadership/readiness loss, or revoked network permission;
in-flight requests drain or follow fenced durable recovery before a replacement pool.
No HTTP cancellation, fivefold speedup, or horizontal scaling is implied. Rare duplicate or overlapping
external calls can occur after crash, pause-before-POST takeover or lost responses;
this is not physical non-overlap or exactly-once billing. Persist request-start and
indeterminate usage evidence, fence stale result writes, and preserve attempt ceilings
and cooldowns. Unknown calls or billing must not be presented as zero.

Each physical request is one recorded attempt; hidden transport retries are disabled.
Persist failure evidence before scheduling a retry. Honor seconds/HTTP-date
`Retry-After`, supported millisecond variants, then the observed provider
`Please retry after N seconds` error form. Missing/invalid delays use a bounded,
versioned exponential fallback. Waiting releases leases and consumes no attempts.
Cooldowns survive restart. Defaults are three attempts, a one-second exponential
fallback capped at 60 seconds, and a maximum accepted provider delay of 24 hours.

## Azure deployment

The hosted-release phase authorizes preparation and validation, not actual provisioning,
cloud migration, restart or paid proof. Deployment requires separate approval and the
proof in `docs/deployment.md`; earlier local/probe success is not hosted-release proof.

Use one existing resource group and preserve its tags, both existing AI Services
accounts, model deployments, projects, endpoints and capacities. The only approved
account changes are publicNetworkAccess Enabled, network ACL default Allow and removal
of operator-IP-only rules, while keeping local authentication disabled.
New hosting/storage/network resources use Sweden Central; the existing MAI account
remains East US. The single-instance Linux App Service has public HTTPS ingress with
no user authentication gate and separately Entra/RBAC-protected SCM. The explicit
MVP mode permits network-reachable SCM without an IP allowlist, while basic SCM/FTP
publishing authentication remains disabled and non-MVP defaults stay restricted.

Hosted coordination uses the Azure Table protocol only after its conformance gates;
private Blob stores immutable datasets and evidence. App Service `/home` is not an
authoritative SQLite database. Local identity uses `DefaultAzureCredential`; hosted
adapters/storage use only `ManagedIdentityCredential`. No keys or connection strings.
Both AI accounts remain publicly network reachable for the portal/browser/playground
and App Service, with actual output-generated endpoints and Entra/MI authentication.
This is not anonymous provider access. Do not create AI private endpoints or AI private
DNS. Only Blob, Table and Key Vault use private endpoints and app VNet integration.
Exact data-plane roles are scoped to both accounts and required containers/table.

The original completed 26x5 pilot is transferred through a reviewed hash-validated
external archive and imported as non-executable archival evidence. Preserve original
manifest/report bytes and every attempt; never recompute the baseline or depend on
the source session directory after import. No dataset/result/archive is committed.

## Acceptance cases

| Case | Input | Expected reference/evidence | Expected behavior |
|---|---|---|---|
| Clean checkout | No external data or credentials | Typed not-configured status | Catalog, synthetic pack, history, health and readiness work |
| Corrupt revision | Generated invalid publication | Typed invalid status | Launch fails before task/provider creation |
| Deterministic pilot | Generated inventory of 2,620 entries | Anchors and 26 ordered strata | Repeated selection is identical and contains 26 unique IDs |
| Safe acquisition | Generated TAR/FLAC members | Hashes and validated pairing | Reject unsafe archives and publish nothing on failure |
| Rate limit | Mock 429 then success | Both raw attempts, delay source, cooldown | Restart preserves wait; other scopes progress |
| Empty success | Mock successful empty transcript | All-reference deletions | Evaluated WER differs from provider failure |
| Unequal coverage | One mocked terminal failure | Partial aggregate and evaluated IDs | No complete-comparison claim |
| Recovery | Restart after acknowledged launch | Same manifest, bytes, tasks and fencing | Complete without the acquisition directory |

## Out of scope

Horizontal worker scaling, marketplace framework, GPUs, self-hosted models, Hugging Face
managed compute, a sixth solution, full 2,620-sample execution, Earnings-21, live
microphone, streaming, diarization, custom speech, batch transcription, audio
conversion/trimming/resampling, universal scoring and UI redesign remain out of scope.
Actual deployment and paid hosted proof require separate authorization after review.
