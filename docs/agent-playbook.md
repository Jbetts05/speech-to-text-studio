# Agent Playbook

## 1. Discover

- Read `AGENTS.md`, `BENCHMARK.md`, `PRODUCT.md`, `DESIGN.md`, and the benchmark
  contract.
- Inventory the repository, installed packs, tests, deployment files, generated assets,
  and current Git state.
- Identify which requirements belong to platform core and which belong to one pack.
- Confirm data rights and use synthetic fixtures until provenance is explicit.

## 2. Plan

- Define the benchmark decision, modality, dataset revision, solutions, pipeline types,
  evaluator, metric families, viewers, pricing provenance, compatibility, and evidence.
- Identify contract changes separately from pack implementation.
- Plan failure, retry, idempotency, resume, retention, accessibility, and deployment
  proof.
- Stop for approval before broad implementation or Azure changes.

## 3. Implement

- Scaffold the pack rather than editing a core registry.
- Keep execution and parsing separate.
- Persist attempt evidence before canonical results.
- Use immutable, content-addressed manifests and artifacts.
- Add focused tests with each behavior.
- Rebuild generated frontend assets only from frontend source.

## 4. Validate locally

- Run the complete quality gates from `AGENTS.md`.
- Run the synthetic offline benchmark end to end.
- Inspect its manifest, attempts, comparison, metrics, artifacts, and report.
- Exercise retry, resume, poison, and finalization paths.
- Verify documentation links and repository hygiene.

## 5. Prepare Azure

- Read the Azure security baseline.
- Confirm deployment is actually in scope.
- Confirm subscription and location.
- Run provider, permissions, Foundry-scope, network, and quota preflight.
- Review Bicep and deployment preview.
- Do not expose or read keys in Entra mode.

## 6. Deploy

- Provision through the documented AZD workflow.
- Deploy the reviewed package.
- Do not compensate for a failed deployment with broad permissions, public access, or
  embedded credentials.

## 7. Prove

- Query ARM for the actual resources and identity.
- Verify the app and SCM/Kudu endpoints.
- Verify `/health` and `/ready`.
- Run a real networked solution call.
- Wait for a completed evaluation.
- Capture the manifest, evidence, metrics, and report identifiers.
- Only then report deployment success.

## 8. Clean up safely

- Remove only temporary local artifacts created by the current work.
- Show cloud resources before proposing deletion.
- Preserve reused resource groups, Foundry accounts, datasets, and run evidence.
- Obtain explicit confirmation before any destructive cloud action.
- Never purge a vault or delete an unknown benchmark revision as routine cleanup.
