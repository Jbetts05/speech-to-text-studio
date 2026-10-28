# Repository instructions

Read and follow [AGENTS.md](../AGENTS.md) before making changes.

- Keep platform core generic and benchmark-specific behavior inside
  `benchmark_packs/<slug>`.
- Preserve immutable manifests, evidence-first execution, retries, idempotency, leases,
  and storage-backend conformance.
- Keep quality, cost, latency, usage, and reliability separate. Never add a universal
  score.
- Follow [DESIGN.md](../DESIGN.md), including the Horizontal Ledger Rule.
- Preserve the platform-owned Evidence Ledger visual and component system. Benchmark
  packs configure terminology, metrics, and renderers; they do not invent a new theme,
  shell, navigation hierarchy, or comparison layout unless the user explicitly requests
  a redesign.
- Follow [docs/azure-security-baseline.md](../docs/azure-security-baseline.md) for Azure
  work and [docs/deployment.md](../docs/deployment.md) for deployment proof.
- Use only synthetic or clearly redistributable fixtures. Never commit secrets, cloud
  IDs, local paths, generated results, or deployment state.
- Run the complete quality gates in `AGENTS.md`.
- Never add a `Co-authored-by` trailer.
