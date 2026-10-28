# Speech-to-Text Studio operations

This is a **26-sample pilot**, not the full LibriSpeech test-clean benchmark. The pack
catalog contains exactly five managed solutions and a complete run contains **130
tasks**. See [BENCHMARK.md](../BENCHMARK.md) for the pinned selection and evaluation
contract. The installed pilot is preserved alongside explicitly selected English uploads.
Neither a paid provider call nor application deployment is implied by passing local
gates. Hosted deployment and its proof require separate authorization.

## Public hosted datasets

The hosted demo has no user authentication or ownership gate. Anyone with its URL can
view published datasets, play/download artifacts and launch paid managed evaluations.
Public access to the website is separate from the managed identity used by the backend
for private Storage and AI services.

Within the existing Benchmarks or Configure run workspace, select a published dataset
or choose matching files for a new one. Each basename must have exactly one FLAC, WAV
or MP3 and one TXT reference. `sample.wav` matches `sample.txt`; `Sample.wav` does not
match `sample.txt`. Up to ten pairs are accepted. Files are English-only, and uploaders
must acknowledge public visibility, rights to share, the language contract and expiry
before sending bytes. Do not upload personal, confidential or unlicensed material.

The maximum is 25,000,000 bytes per audio, 16,384 bytes per UTF-8 reference and
250,000,000 aggregate file-data bytes. Each audio must be no longer than five minutes,
and the dataset no longer than thirty minutes. Actual content, metadata and full
bounded decode are checked; a supported filename alone is not sufficient. There are
no archives, remote URLs, conversion, resampling, trimming or microphone input.

The browser retains a direct multi-file selection experience but uses a durable
multi-request session. Small admission JSON reserves each file's exact name, length
and SHA-256. Separate, bounded raw file requests pass through the app, not through a
browser SAS or private Blob URL. Status identifies completed parts so interrupted
transfers resume without republishing a dataset. Finalize verifies exact parts/totals
and schedules asynchronous validation. Only complete validation publishes a revision.
Incomplete sessions expire and are reaped.

Choose the ready revision and select solutions. In the enabled hosted runtime, Launch
explicitly authorizes their paid calls without another checkbox or modal; a passive
note explains charges and retries. Local execution retains its network opt-in.
Hosted execution has at most three total attempts per task. There is
no cumulative launch limit or daily/monthly spending cap. A temporarily full queue,
receiver pool or request-rate window returns 429 and Retry-After; it does not create
another run when the same accepted idempotency key is replayed.

New speech runs pin up to five concurrent file/solution tasks globally across runs,
with one task per solution. Fast lanes advance independently of a blocked file/service;
there is no per-file barrier. Older manifests remain serial. MAI versions still share
their endpoint/service cooldown; one lane per solution is not a quota guarantee.
Provider-request latency, full attempt duration, queue/cooldown waits and run elapsed
time remain separate, and concurrency does not promise a fivefold speedup.

The user-selected availability policy keeps one current-generation coordinator with
bounded concurrent provider senders. Admission stops on shutdown, authorization,
readiness or leadership loss; already-running requests drain or recover honestly.
Restart in the same process cannot create an overlapping replacement executor.
Rare duplicate or overlapping external calls may occur after crash, pause-before-POST
takeover or lost responses. Request-start and indeterminate usage evidence remain
durable; stale result writes are fenced and attempt ceilings/cooldowns are preserved.
This is not a promise of physical non-overlap or exactly-once execution/billing.

## Ten-day expiry and preserved baseline

Published uploads expire ten days after their first publication, and every derived
run, report, attempt and artifact shares that deadline. Launching on the ninth day does
not retain the original audio for another ten days. Reads, retries and exports do not
renew expiry. New ordinary runs on the permanently installed dataset expire ten days
after acceptance.

The installed LibriSpeech 26 revision and designated original completed 26x5 pilot are
the only permanent baseline pins. Public requests cannot pin a dataset or set their
own expiry. A reviewed archive import preserves the baseline's original manifest/report
bytes, all attempts and hashes; it never reruns historical tasks.

Expiry is checked on catalogs, runs, reports and artifact GET/HEAD/range/conditional
requests even if cleanup is delayed. A resumable reaper also removes operational
indexes, references, unshared physical objects and temporary files. Shared content
still required by a live dataset or the pinned baseline remains protected. Uploaded
content has no extra Blob soft-delete/version-history recovery window. Cleanup resumes
after outages; previously downloaded copies and provider-side retention cannot be
recalled.

## Clean checkout

Follow the virtual-environment setup in [README.md](../README.md). The locked environment
includes the optional speech dependencies. A minimal installed application can discover
both packs without importing provider clients, acquiring credentials, or reading data.
Install the `speech` extra in a virtual environment before acquiring audio or invoking
managed speech.

```powershell
ai-bench validate --brief BENCHMARK.md
ai-bench run synthetic_text_intent
ai-bench serve
```

The speech catalog reports `not-configured` until an external revision is selected.
Selecting a missing, partial, or corrupt revision reports `invalid`, distinctly from
absence of configuration. These conditions do not disable the synthetic pack, history,
health, or readiness. Launch fails before tasks or provider calls when the revision is
not ready. No empty dataset is substituted.

## External acquisition and publication

Acquisition is separate from solution execution and is never implicit. The following
commands are for an operator who separately chooses to acquire the official dataset.
All paths must be outside the checkout; links and repository-local destinations are
rejected. Do not put the dataset under `benchmark_packs`.

```powershell
$archive = Read-Host 'External archive destination'
$publications = Read-Host 'External publication directory'
python -m benchmark_packs.speech_to_text.dataset acquire --destination $archive
python -m benchmark_packs.speech_to_text.dataset publish --archive $archive --output $publications
```

The source is [OpenSLR SLR12](https://www.openslr.org/12), licensed CC BY 4.0. The official
`test-clean.tar.gz` MD5 is `32fa31d27d2e1cad72775fee3f4849a9`, from the
[upstream checksum list](https://www.openslr.org/resources/12/md5sum.txt). HTTPS and the
upstream checksum establish the acquisition baseline; SHA-256 identities are also
computed for the archive, inventory, selection, samples, input bytes, and references.
The MD5 is an upstream integrity checksum, not a claim of modern cryptographic strength.

Publication validates an isolated archive copy: safe members, no links or special files,
no duplicate/case collisions, exact utterance/reference pairing, bounded expansion,
FLAC STREAMINFO, decoded PCM frame count, and FLAC PCM checksum. All 2,620 utterances
are validated. Only the selected 26 original audio/reference objects are needed in the
published pilot; the complete inventory and deterministic selector evidence are retained.
Publication is an atomic directory rename. Existing immutable revisions are compared,
never replaced. Interrupted temporary publications are not discoverable revisions.

The selector orders exact frame/rate durations, not rounded seconds. Its seed/version,
rank-zero/lower-median/longest anchors, 26 strata, speaker/chapter balance, hashes,
upstream attribution, and coverage are persisted in the publication. It does not read
reference difficulty or provider outcomes when selecting samples.

Set the full revision directory returned by publication, then restart the local server:

```powershell
$env:AI_BENCH_SPEECH_DATASET_REVISION = Read-Host 'Published external revision directory'
```

## Explicit Entra provider configuration

Provider configuration is a separate, external UTF-8 JSON file with exactly these keys:

| Key | Meaning |
|---|---|
| `schema_version` | `"1"` |
| `speech_endpoint` | Actual Sweden Central AI Services endpoint for standard fast transcription |
| `openai_endpoint` | Actual AI Services/OpenAI endpoint hosting the two managed Audio deployments |
| `mai_endpoint` | Actual East US AI Services endpoint for MAI |
| `gpt_transcribe_deployment` | Explicit deployment name; model `gpt-transcribe`, version `2026-07-28` |
| `whisper_deployment` | Explicit deployment name; model `whisper`, version `001` |

No endpoint or deployment name is derived. Credentials, API keys, connection strings,
unknown keys, endpoint query strings, and embedded URL credentials are not accepted.
Do not commit this file. Configure its location before starting the server:

```powershell
$env:AI_BENCH_SPEECH_PROVIDER_CONFIG = Read-Host 'External provider configuration file'
$env:AI_BENCH_AUTH_MODE = 'entra'
```

Local execution uses `DefaultAzureCredential`. Hosted execution with
`AI_BENCH_RUNTIME=azure` uses `ManagedIdentityCredential`, optionally selecting
`AI_BENCH_AZURE_MANAGED_IDENTITY_CLIENT_ID`. Hosted mode never uses a broad credential fallback or reads the local configuration
file. Its complete non-secret settings come from actual Bicep existing-resource outputs
and validated app settings, never inferred endpoints. Follow
[the Azure security baseline](azure-security-baseline.md) for scoped RBAC, private
backend connectivity, public main-site ingress and separately restricted SCM. Repository
implementation does not itself authorize deployment or role changes.

## Request contracts

| Solution | API | Request options |
|---|---|---|
| Azure Speech standard fast | Speech `2025-10-15` | `locales=["en-US"]` |
| MAI-Transcribe-1.5 | Speech `2025-10-15` | `locales=["en"]`, `enhancedMode.enabled=true`, model `MAI-Transcribe-1.5` |
| MAI-Transcribe-2 | Speech `2025-10-15` | Same enhanced mode, model `MAI-Transcribe-2` |
| gpt-transcribe | Managed Audio `2025-03-01-preview` | Explicit deployment, `language=en` |
| Managed whisper | Managed Audio `2025-03-01-preview` | Explicit deployment, `language=en` |

Speech uses `/speechtotext/transcriptions:transcribe` with multipart `audio` and
`definition`; managed Audio uses `/openai/deployments/{deployment}/audio/transcriptions`
with multipart `file` and `language`. All five upload the validated original bytes with
their actual audio media type and an opaque filename. The installed pilot remains
original `audio/flac`; uploads may also use WAV or MP3. No conversion, trimming,
resampling, references, original user filenames, prompts or diarization options are sent.
Speech parsing concatenates `combinedPhrases[*].text`; managed Audio parsing reads `text`.
An empty valid transcript is a success, not a provider error.

## Durable launch and recovery

Networked execution requires current `AI_BENCH_ALLOW_NETWORKED_SOLUTIONS=true` and an
explicit network authorization in the API request. For a known enabled hosted runtime,
the Launch button supplies `allow_networked=true`; local operation retains its
checkbox. The hosted public deployment intentionally permits that per-run choice after the
operator has enabled the reviewed release. Nothing in discovery, acquisition, upload,
validation or archival import enables a provider call implicitly.

`POST /api/runs` returns **202** after input/reference objects, a selected-sample
snapshot, manifest, and recoverable tasks are persisted. An `Idempotency-Key` replay
returns the same immutable run and repairs interrupted initialization. Reports remain
unavailable with HTTP 409 until finalization. Poll run detail/events rather than
reposting a launch when a response is lost.

A new key is reserved only after pack, dataset/artifact, solution, authorization, and
manifest validation succeeds. A rejected request leaves the key available for a corrected
request. Existing reservations still enforce their original request identity; existing
run replay remains independent of current external dataset availability.

A FastAPI-lifespan supervisor owns one renewable backend worker lease, recovers expired
task leases, repairs task initialization, and finalizes fenced results. Local execution
uses SQLite/filesystem; hosted execution requires the conformant Table/Blob backend,
not SQLite on the App Service network share. It is not a
request-scoped background task. Restart resumes pending work. Due-time waits and wakeups
replace busy polling. Runtime network authorization is rechecked before every request;
disabling it pauses work without consuming provider attempts.

After authorization is restored, either driver revalidates the manifest, persisted
input/reference artifacts, and current network policy before clearing a resolved runtime
block and claiming pending tasks. Failed integrity or authorization checks leave the
block intact.

A restart can finalize already-terminal tasks with network execution disabled; no
reauthorization is needed solely to publish the local report. Orphaned version 1 launch
reservations are repaired only after their legacy descriptor fingerprint is validated.
The original reserved run ID survives the upgrade, and changed requests remain conflicts.

Version 2 manifests pin the content-addressed selected snapshot, non-secret provider
settings and endpoint identities, API/deployment/model selectors, locale, request
options, normalization, retry policy, and renderer/evaluator/pricing descriptors.
Execution reads persisted objects, never the acquisition directory. Version 1 manifests
remain readable and retain their legacy drift checks. Provider selectors do not make a
provider's backend weights immutable.

## Retries and cooldowns

Hidden HTTP retries and redirects are disabled. Safe failure evidence retains status,
request ID, raw response/media type when available, classification, known usage with
explicit uncertainty, and requested/scheduled delay. Evidence is persisted before a
retry. Sensitive values or URLs echoed in failed bodies are redacted and marked.

Delay precedence is `Retry-After` seconds or HTTP-date, supported millisecond headers,
then `Please retry after N seconds` in the provider error message. Missing/invalid delays
use the pinned bounded exponential fallback. No observed probe delay is hard-coded.

Cooldown deadlines and failure evidence are committed together, so a crash before retry
scheduling does not lose the delay. Waiting tasks release leases and do not consume
attempts. Opaque scopes separate managed deployments and Speech accounts; MAI selectors
on the same Speech account share a throttle scope. Other scopes can progress. Failed
requests may be billable: billing never silently becomes zero.

## Evaluation and evidence

`librispeech-english-v1` applies Unicode compatibility normalization, case folding,
apostrophe normalization, punctuation separation, and whitespace tokenization. Internal
apostrophes/contractions remain intact. Numerals remain literal; `21` is not guessed to
mean `twenty one`. Equal-cost alignment steps prefer match, substitution, deletion,
then insertion.

Per-sample evidence includes original strings, normalized tokens, deterministic alignment,
S/D/I counts, reference words, and WER. Corpus WER sums word errors and divides by total
evaluated reference words; it is not mean sentence WER and may exceed 100%. Successful
empty output is all deletions. Provider failures do not become empty hypotheses.

Reports carry expected/evaluated case IDs, completeness, and coverage comparability.
Partial quality stays visible, but incomplete or different coverage is not ranked as a
complete comparable result. Original regional price snapshots were not priced: all five
recorded pilot costs remain explicitly unknown, separate from the current estimates
below. Provider request time, attempt time, queue wait, cooldown
wait, and end-to-end elapsed time have separate fields.

### Current-rate cost estimates

Cost is always visible in evaluation, per-sample comparison and Reports. Original
recorded amounts are separate from dated current-rate estimates. Both retain currency
and attempt/task coverage; a known subtotal is not a complete run total, missing prices
or usage are not zero, and mixed currencies are never summed. Pricing disclosures show
original snapshots, reviewed rates/sources, effective dates, assumptions and recorded
usage across retries, including failed submissions.

The pack's read-only pricing provider reviewed the public
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
on **2026-09-19**, selecting USD Consumption tier-zero hour meters:

| Pinned solution/model | Region and meter | USD/hour | Effective |
|---|---|---|---|
| Standard Speech fast | Sweden Central, Azure Speech / Fast Transcription | 0.36 | 2024-11-01 |
| GPT-Transcribe `2026-07-28` | Sweden Central, Azure OpenAI Media / gpt-transcribe Gl | 0.27 | 2026-08-01 |
| Managed Whisper `001` | Sweden Central, Azure OpenAI / Speech-to-Text-Batch-Whisper-regnl | 0.4356 | 2026-03-01 |
| MAI-Transcribe-1.5 and MAI-Transcribe-2 | East US, Azure Speech / Fast Transcription | 0.36 | 2024-11-01 |

[Microsoft Speech pricing footnote 9](https://azure.microsoft.com/en-us/pricing/details/speech/)
explicitly assigns LLM Speech the Fast Transcription price/SKU.
[MAI documentation](https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe)
identifies the enhanced-mode selectors. The
[OpenAI pricing page](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)
lists GPT-Transcribe Global audio-hour pricing separately from GPT-4o-transcribe token
pricing. Each UI quote links the exact regional Retail API filter and pricing page.
No other-model, data-zone, global Whisper, promotional or free-tier rate is substituted.

The calculation is `sum(recorded submitted_audio_seconds / 3600 * list rate)`.
It is **conditional, pro-rata and not billed cost**: provider usage/billability,
minimums and rounding can differ; discounts, promotions and taxes are excluded.
GPT uses the declared GlobalStandard profile and Whisper the declared Standard profile.
Historical manifests do not pin those deployment tiers, so each quote explicitly
states the assumption; a pinned incompatible tier/model/version/region yields unknown
rather than a fallback. Failed/retried recorded submissions remain included even if
not billed. Unobserved request starts or missing durations make coverage partial.
The original unknown-price adapter snapshots, manifests, results and JSON/CSV exports
are never repriced or rewritten. No pricing or provider network call occurs on viewing.

The Evidence Ledger keeps canonical audio/transcript comparisons primary and
retry/provider/raw evidence in Operational details. Native controls do not autoplay.
Backend-generated frontend wire types are checked for drift by the Python suite:

```powershell
python scripts/generate_api_types.py
python scripts/generate_api_types.py --check
```

## Infrastructure and deferred work

The speech probe Bicep now references an explicitly existing resource group and never
writes its tags. Creating a dedicated group is a separate operator-approved action;
targeting a nonexistent group fails closed. This repository change performs no Azure
resource operations.

The hosted release modifies the existing AZD/Bicep composition for one existing group,
one App Service and private Blob/Table/Vault. Both existing AI accounts use the
explicitly approved publicNetworkAccess Enabled/default-Allow network ACL posture,
without operator-IP-only rules, so portal/browser/playground and app access work.
Preserve tags, names, custom subdomains, endpoints, disabled local authentication,
identity, encryption, deployments, child projects, capacities and other account settings.
Inference still uses Entra/managed identity; public network reachability does not mean
anonymous provider access. There are no AI private endpoints or AI private DNS.
Do not use the resource-creating speech-probe template for hosting. The eventual
what-if must show only the approved existing-account network modifications.

The baseline transfer archive and all cloud state stay external. After import,
restarts and future deployments use only Blob/Table, not the original session or an
external acquisition directory. Real backend conformance, identity/network checks,
baseline hashes, native playback, restart persistence, public upload and a bounded
five-solution proof are required before claiming deployment success.

Horizontal scaling, a marketplace framework, GPUs, self-hosted/managed compute, a sixth
solution, full-dataset execution, Earnings-21, microphones, streaming, diarization,
custom speech, batch transcription, universal scores and UI redesign remain out of
scope. Actual deployment and paid proof require separate approval after review.
