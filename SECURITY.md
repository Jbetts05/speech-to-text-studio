# Security Policy

Report suspected vulnerabilities privately through the repository's GitHub security
advisory feature. Do not open a public issue containing credentials, exploitable sample
data, signed artifact URLs, tenant details, or cloud resource identifiers.

The supported branch is `main`. Security fixes must preserve the identity, private
networking, evidence integrity, and no-secret clean-checkout requirements in
[AGENTS.md](AGENTS.md) and
[docs/azure-security-baseline.md](docs/azure-security-baseline.md).

The included CodeQL workflow is manual by default because private repositories require
GitHub Code Security entitlement to upload code-scanning results. After confirming that
entitlement, adopters may enable push, pull-request, or scheduled triggers.
