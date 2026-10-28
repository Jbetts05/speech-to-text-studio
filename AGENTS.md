# Agent Instructions

This file is authoritative for all work in this repository. Read it before changing
code, configuration, tests, documentation, infrastructure, or generated assets.

## Required workflow

1. Read `BENCHMARK.md`, `PRODUCT.md`, `DESIGN.md`,
   `docs/benchmark-contract.md`, and the relevant deployment documents.
2. Inspect existing contracts and prior art before adding a new abstraction.
3. Write a concrete plan for changes that cross platform, pack, UI, or infrastructure
   boundaries.
4. Implement the smallest complete change that preserves all invariants.
5. Add or update contract, behavior, recovery, accessibility, and documentation tests.
6. Run the quality gates below from a clean working tree.
7. Audit the diff for secrets, generated results, identifiers, copied data, stale
   generated assets, and unrelated changes.
8. For deployment work, follow `docs/deployment.md` and record proof. Do not infer
   success from an Azure Developer CLI status message.

## Architecture boundaries

- `src/ai_bench` is generic platform core. It must not know a benchmark pack by name.
- `benchmark_packs/<slug>` owns benchmark terminology, fixtures, schemas, adapters,
  evaluators, viewers, pricing records, compatibility, and configuration.
- Packs are discovered through the pack contract. Do not add pack-specific imports,
  branches, labels, or registry constants to platform core.
- Dataset acquisition and validation are separate from solution execution.
- Solution execution and benchmark-specific parsing are separate operations.
- Evaluators emit declared metrics and evidence. They do not create a universal score.
- Quality, cost, latency, usage, and reliability are independent metric families.
- `standalone` and `assisted` pipelines are explicit configurations. Do not infer a
  pipeline from adapter names or implementation details.
- Run manifests are immutable. Corrections create a new run or revision.
- Raw and parsed attempt evidence is persisted before canonical results.
- Any storage backend promoted to active execution must implement the same protocols and
  pass the lease/fencing conformance suite first.
- Frontend API types must be generated or contract-tested against backend schemas.

## Prohibited shortcuts

- Do not copy datasets, binary fixtures, prompts, schemas, provider catalogs, pricing
  tables, generated results, deployment state, cloud identifiers, or rights claims from
  another project.
- Do not commit `.env` files, credentials, access keys, connection strings, tenant or
  subscription IDs, resource endpoints, or user-specific absolute paths.
- Do not make unknown pricing zero, reuse another model's price, or omit retry usage.
- Do not combine metric families into a winner, rank, grade, or universal score.
- Do not bypass dataset package validation, manifest hashing, compatibility checks,
  evidence persistence, idempotency, leases, or finalization fencing.
- Do not make a networked provider the default or let hosted code read service keys in
  Entra mode.
- Do not hand-edit generated frontend assets.
- Do not weaken tests, typing, security, accessibility, or deployment checks to make a
  change pass.
- Do not claim a real deployment is successful without the required proof run.
- Do not change or remove resources during cleanup unless the user approved the exact
  destructive action.

## Quality gates

Run the commands that apply to the change. Before delivery, run all of them.

```powershell
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest
python -m build
python scripts/check_wheel.py
Push-Location frontend
npm ci
npm audit --audit-level=high
npx playwright install chromium
npm test
npm run typecheck
npm run build
npm run test:e2e
Pop-Location
az bicep build --file infra/main.bicep --stdout
python -m infra.scripts.stage_package --dry-run
python scripts/check_docs.py
python scripts/check_repository_hygiene.py
git diff --exit-code -- src/ai_bench/web/static
```

A clean checkout must pass without a secret, Azure subscription, paid service, or
networked model call.

## Frontend and accessibility

- Follow `DESIGN.md`.
- The Evidence Ledger shell, navigation, tokens, spacing, evaluation hierarchy,
  responsive breakpoints, and core components are platform-owned. New benchmark packs
  inherit them unchanged. Do not invent a new theme, dashboard, navigation model, or
  comparison layout unless the user explicitly requests a platform redesign.
- Packs may supply terminology, viewer descriptors, metric definitions, and
  modality-specific renderers only through the documented extension slots.
- Primary evaluation views use result-level comparison evidence. Raw, parsed, provider,
  and attempt artifacts belong in operational detail views and must not flood the main
  comparison surface.
- Use semantic HTML before ARIA.
- Preserve keyboard operation, focus visibility, non-color status cues, reduced motion,
  and screen-reader labels.
- Every horizontally scrollable table must satisfy the Horizontal Ledger Rule and its
  rendered-bound tests at `scrollLeft` zero and nonzero.

## Azure requirements

- Follow `docs/azure-security-baseline.md`.
- Hosted authentication defaults to managed identity and Entra ID.
- Key fallback is local and explicit.
- Storage and Key Vault public access stay disabled.
- Existing Foundry resources may be reused across resource groups; assign
  `Cognitive Services User` at the actual account scope.
- Preserve existing resource-group tags and optional deployment configuration.
- Run provider-registration, role-assignment, quota, health, readiness, Kudu, and proof
  preflight checks as documented.

## Generated artifacts and cleanup

- Generated frontend assets are committed and reproducible.
- Run outputs, caches, local storage, deployment packages, and Azure environment state
  are ignored.
- Cleanup is limited to artifacts created by the current operation. Never delete an
  unknown resource, user dataset, benchmark revision, run, or workspace.
- Soft-deleted Key Vault secrets are recovered only during an explicit switch back to
  key mode; Entra mode removes stale provider-key secrets without reading them.

## Git rules

- Use only the configured Git author and committer identity.
- Never add a `Co-authored-by` trailer.
- Do not amend, rewrite, force-push, or alter unrelated user changes unless explicitly
  requested.
- Keep commits reviewable and include validation evidence in the pull request.
