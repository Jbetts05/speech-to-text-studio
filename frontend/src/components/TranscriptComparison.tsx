import type { BackendTranscriptComparison } from "../api/contracts.generated";
import type { Evidence, ViewerDescriptor } from "../types";
import { ArtifactValue, useArtifactContent } from "./ArtifactPreview";
import { formatMetricValue } from "../format";

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function isTranscriptComparison(value: unknown): value is BackendTranscriptComparison {
  if (!isRecord(value)) return false;
  return (
    value.kind === "transcript-alignment" &&
    value.schema_version === "1" &&
    value.normalization_version === "librispeech-english-v1" &&
    typeof value.reference_text === "string" &&
    typeof value.hypothesis_text === "string" &&
    Array.isArray(value.reference_tokens) &&
    value.reference_tokens.every((token) => typeof token === "string") &&
    Array.isArray(value.hypothesis_tokens) &&
    value.hypothesis_tokens.every((token) => typeof token === "string") &&
    ["substitutions", "deletions", "insertions", "reference_words"].every(
      (key) => typeof value[key] === "number" && Number.isInteger(value[key]) && value[key] >= 0,
    ) &&
    typeof value.word_error_rate === "string" &&
    value.word_error_rate.trim() !== "" &&
    Number.isFinite(Number(value.word_error_rate)) &&
    Array.isArray(value.alignment) &&
    value.alignment.every(
      (word) =>
        isRecord(word) &&
        ["match", "substitution", "deletion", "insertion"].includes(String(word.operation)) &&
        (typeof word.reference === "string" || word.reference === null) &&
        (typeof word.hypothesis === "string" || word.hypothesis === null),
    )
  );
}

function TranscriptAlignment({
  evidence,
  artifactUrl,
}: {
  evidence?: Evidence;
  artifactUrl: (sha256: string) => string;
}) {
  const artifact = useArtifactContent(evidence, artifactUrl);
  if (!evidence) return <span>No evaluated transcript</span>;
  if (artifact.status === "loading") return <span>Loading comparison...</span>;
  if (artifact.status !== "loaded" || !isTranscriptComparison(artifact.content)) {
    return <span>Transcript comparison unavailable. Inspect the comparison artifact in operational details.</span>;
  }
  const comparison = artifact.content;
  return (
    <div className="transcript-comparison">
      <p className="transcript-metrics">
        <strong>
          WER {formatMetricValue({
            metric_id: "word_error_rate", family: "quality",
            value: comparison.word_error_rate, unit: "ratio",
          })}
        </strong>
        <span>S {comparison.substitutions} / D {comparison.deletions} / I {comparison.insertions}</span>
        <span>{comparison.reference_words} reference words</span>
      </p>
      {comparison.hypothesis_tokens.length === 0 && <p>Empty successful transcript</p>}
      <ol className="transcript-alignment" aria-label="Normalized transcript alignment">
        {comparison.alignment.map((word, index) => (
          <li key={index} className={`word-${word.operation}`}>
            {word.operation === "match" ? (
              <span>{word.hypothesis}<span className="sr-only"> matched</span></span>
            ) : (
              <>
                <small>{word.operation}</small>
                {word.reference !== null && <del>{word.reference}</del>}
                {word.hypothesis !== null && <ins>{word.hypothesis}</ins>}
              </>
            )}
          </li>
        ))}
      </ol>
      <details className="transcript-original">
        <summary>Original hypothesis</summary>
        <p>{comparison.hypothesis_text || "(empty)"}</p>
        <small>{comparison.normalization_version}</small>
      </details>
    </div>
  );
}

export function ResultComparison({
  evidence,
  viewer,
  artifactUrl,
}: {
  evidence?: Evidence;
  viewer?: ViewerDescriptor;
  artifactUrl: (sha256: string) => string;
}) {
  return viewer?.id === "transcript-alignment" ? (
    <TranscriptAlignment evidence={evidence} artifactUrl={artifactUrl} />
  ) : (
    <ArtifactValue evidence={evidence} artifactUrl={artifactUrl} />
  );
}
