import { useEffect, useMemo, useState } from "react";
import type { Evidence, ViewerDescriptor } from "../types";
import { DownloadIcon } from "./Icons";
import { assertArtifactResponse, checkMediaExpiry } from "../api/expiry";

export function useArtifactContent(
  evidence: Evidence | undefined,
  artifactUrl: (sha256: string) => string,
) {
  const [state, setState] = useState<
    | { status: "idle" | "loading"; content?: undefined }
    | { status: "loaded"; content: unknown }
    | { status: "error"; content?: undefined }
  >({ status: "idle" });

  const artifactSha256 = evidence?.artifact_sha256;
  const modality = evidence?.modality;
  const mediaType = evidence?.media_type;
  const hasEvidence = evidence !== undefined;
  const url = artifactSha256
    ? artifactUrl(artifactSha256)
    : undefined;

  useEffect(() => {
    setState({ status: hasEvidence ? "loading" : "idle" });
    if (!hasEvidence || !url) return;
    if (!["text", "structured"].includes(modality ?? "")) {
      setState({ status: "loaded", content: url });
      return;
    }
    const controller = new AbortController();
    void fetch(url, {
      signal: controller.signal,
      headers: { Accept: mediaType ?? "*/*" },
    })
      .then(async (response) => {
        if (controller.signal.aborted) return;
        assertArtifactResponse(response, url);
        const text = await response.text();
        const responseMediaType =
          response.headers.get("Content-Type") ?? mediaType;
        if (controller.signal.aborted) return;
        setState({
          status: "loaded",
          content: responseMediaType?.includes("json") ? JSON.parse(text) : text,
        });
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setState({ status: "error" });
        }
      });
    return () => controller.abort();
  }, [hasEvidence, mediaType, modality, url]);

  return { ...state, url };
}

function displayContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (content === undefined) return "";
  return JSON.stringify(content, null, 2);
}

export function ArtifactValue({
  evidence,
  artifactUrl,
  property,
}: {
  evidence?: Evidence;
  artifactUrl: (sha256: string) => string;
  property?: string;
}) {
  const artifact = useArtifactContent(evidence, artifactUrl);
  const value = useMemo(() => {
    if (artifact.status !== "loaded") return undefined;
    if (
      property &&
      artifact.content !== null &&
      typeof artifact.content === "object" &&
      !Array.isArray(artifact.content) &&
      property in artifact.content
    ) {
      return (artifact.content as Record<string, unknown>)[property];
    }
    return artifact.content;
  }, [artifact, property]);

  if (!evidence) return <span className="preview-empty">Not recorded</span>;
  if (artifact.status === "loading") return <span>Loading…</span>;
  if (artifact.status === "error") return <span>Preview unavailable</span>;
  return <span>{displayContent(value)}</span>;
}

export function ArtifactPreview({
  evidence,
  viewer,
  artifactUrl,
  onInspect,
}: {
  evidence?: Evidence;
  viewer?: ViewerDescriptor;
  artifactUrl: (sha256: string) => string;
  onInspect: (evidence: Evidence, trigger: HTMLButtonElement) => void;
}) {
  const artifact = useArtifactContent(evidence, artifactUrl);
  const modality = evidence?.modality ?? viewer?.modality ?? "unknown";

  if (!evidence) {
    return (
      <div className="preview-empty-state">
        <span>No artifact selected</span>
        <p>Choose a completed sample to inspect immutable evidence.</p>
      </div>
    );
  }

  return (
    <article className={`artifact-preview artifact-preview-${modality}`}>
      <div className="artifact-preview-stage">
        {artifact.status === "loading" && <p role="status">Loading evidence…</p>}
        {artifact.status === "error" && (
          <p role="alert">The inline preview could not be loaded.</p>
        )}
        {artifact.status === "loaded" && modality === "image" && artifact.url && (
          <img src={artifact.url} alt={evidence.summary ?? evidence.label} />
        )}
        {artifact.status === "loaded" && modality === "audio" && artifact.url && (
          <audio src={artifact.url} controls preload="metadata" aria-label={evidence.label} onError={() => void checkMediaExpiry(artifact.url!)} />
        )}
        {artifact.status === "loaded" && modality === "video" && artifact.url && (
          <video src={artifact.url} controls preload="metadata" onError={() => void checkMediaExpiry(artifact.url!)} />
        )}
        {artifact.status === "loaded" && modality === "document" && (
          <div className="artifact-preview-message">
            <strong>Document preview unavailable</strong>
            <span>
              Use Inspect artifact to review metadata and download the file
              safely.
            </span>
          </div>
        )}
        {artifact.status === "loaded" &&
          ["text", "structured"].includes(modality) && (
            <pre>{displayContent(artifact.content)}</pre>
          )}
      </div>
      <footer>
        <span>{evidence.media_type ?? viewer?.media_types[0] ?? modality}</span>
        <button
          type="button"
          className="text-button"
          onClick={(event) => onInspect(evidence, event.currentTarget)}
        >
          <DownloadIcon />
          Inspect artifact
        </button>
      </footer>
    </article>
  );
}
