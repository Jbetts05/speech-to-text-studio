# AI Bench Product

## Purpose

AI Bench helps engineering, product, procurement, and governance teams compare AI
models and services using reproducible evidence rather than a single opaque ranking.
Each benchmark pack defines one task, its accepted inputs and references, compatible
solutions, evaluation logic, artifact presentation, and pricing provenance.

## Users

- Benchmark authors create datasets, adapters, evaluators, and viewers.
- Reviewers inspect per-case evidence and aggregate metrics.
- Platform operators run durable local or hosted evaluations.
- Decision makers compare tradeoffs without losing metric provenance.
- Auditors trace every reported value to an immutable manifest and artifact.

## Product invariants

1. Every run pins a benchmark definition, dataset revision, solution configuration,
   evaluator version, pricing snapshot, and compatibility declaration.
2. Input, reference, raw response, parsed output, comparison, telemetry, and report
   artifacts have stable evidence references and hashes.
3. A retry never erases the evidence or usage of an earlier attempt.
4. Unsupported and partially comparable solutions are visible, never normalized into
   false equivalence.
5. Unknown values remain unknown.
6. Offline operation requires no secret or paid service.
7. Networked execution is explicit and attributable.
8. Accessibility is a correctness requirement.

## Metric separation

AI Bench groups metrics into independent families:

| Family | Examples | Rule |
|---|---|---|
| Quality | Accuracy, F1, error rate, task-specific evidence | Defined by the benchmark evaluator |
| Cost | Billed amount, estimated amount, unknown price | Uses a versioned pricing snapshot |
| Latency | Attempt, component, queue, and end-to-end duration | Retry latency remains attributable |
| Usage | Tokens, pages, seconds, calls, bytes, custom units | Declared by each solution component |
| Reliability | Success, timeout, retry, parse failure, poison state | Never hidden by a final successful retry |

The product does not calculate a universal score. Consumers may filter or visualize
metric families, but the platform does not decide how to trade one family against
another.

## Execution models

- A `standalone` pipeline produces the result from one declared solution.
- An `assisted` pipeline declares every upstream and downstream component, including
  helper models or preprocessing services, and attributes usage, cost, latency, and
  failures to each component.

The pipeline type is configuration, not a label inferred from branding.

## Evidence-first reporting

Aggregate reports are navigation layers over evidence, not substitutes for it. A
reviewer can move from an aggregate metric to a case, comparison, parsed value, raw
attempt, input/reference artifact, manifest, and pricing record.
