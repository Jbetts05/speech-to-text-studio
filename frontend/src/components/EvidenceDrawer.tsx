import { useEffect, useRef, useState } from "react";
import type { Evidence } from "../types";
import { assertArtifactResponse } from "../api/expiry";

const modalityDescriptions: Record<string, string> = {
  image: "Image preview. If the preview is unavailable, download the original artifact.",
  document: "Document artifacts are not embedded. Download the original file to inspect it safely.",
  audio: "Audio artifacts are not played automatically. Download the original file to inspect it.",
  video: "Video artifacts are not played automatically. Download the original file to inspect it.",
  unknown: "This artifact type cannot be previewed. Download the original file to inspect it.",
};

function humanBytes(value?: number): string {
  if (value === undefined) return "Size unavailable";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function EvidenceDrawer({
  evidence,
  artifactUrl,
  onClose,
}: {
  evidence?: Evidence;
  artifactUrl: (sha256: string) => string;
  onClose: () => void;
}) {
  const drawerRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [imageFailed, setImageFailed] = useState(false);
  const [loadedContent, setLoadedContent] = useState<unknown>();
  const [loadedMediaType, setLoadedMediaType] = useState<string>();
  const [contentState, setContentState] = useState<
    "idle" | "loading" | "loaded" | "error"
  >("idle");

  useEffect(() => {
    if (!evidence) return;
    setImageFailed(false);
    closeRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = [
        ...drawerRef.current.querySelectorAll<HTMLElement>(
          'button, a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ].filter((node) => !node.hasAttribute("disabled"));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [evidence, onClose]);

  const downloadUrl = evidence?.artifact_sha256
    ? artifactUrl(evidence.artifact_sha256)
    : undefined;

  useEffect(() => {
    setLoadedContent(undefined);
    setLoadedMediaType(undefined);
    setContentState("idle");
    if (
      !evidence ||
      evidence.content !== undefined ||
      !downloadUrl ||
      !["text", "structured"].includes(evidence.modality)
    ) {
      return;
    }
    const controller = new AbortController();
    setContentState("loading");
    void fetch(downloadUrl, {
      signal: controller.signal,
      headers: { Accept: evidence.media_type ?? "*/*" },
    })
      .then(async (response) => {
        if (controller.signal.aborted) return;
        assertArtifactResponse(response, downloadUrl);
        const length = Number(response.headers.get("Content-Length"));
        if (Number.isFinite(length) && length > 2 * 1024 * 1024) {
          throw new Error("Artifact is too large for an inline preview");
        }
        const text = await response.text();
        if (controller.signal.aborted) return;
        const mediaType = response.headers.get("Content-Type") ?? "";
        setLoadedMediaType(mediaType.split(";", 1)[0] || undefined);
        setLoadedContent(
          mediaType.includes("json") ? JSON.parse(text) : text,
        );
        setContentState("loaded");
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setContentState("error");
        }
      });
    return () => controller.abort();
  }, [downloadUrl, evidence]);

  if (!evidence) return null;
  const previewContent = evidence.content ?? loadedContent;
  const previewMediaType = loadedMediaType ?? evidence.media_type;
  const structuredPreview =
    evidence.modality === "structured" ||
    previewMediaType?.includes("json") === true ||
    (previewContent !== null && typeof previewContent === "object");

  return (
    <div className="drawer-layer">
      <button
        type="button"
        className="drawer-scrim"
        aria-label="Close evidence inspector"
        onClick={onClose}
      />
      <div
        className="evidence-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evidence-drawer-title"
        aria-describedby="evidence-drawer-summary"
        ref={drawerRef}
      >
        <header>
          <div>
            <span className="drawer-kicker">{evidence.modality} evidence</span>
            <h2 id="evidence-drawer-title">{evidence.label}</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close evidence inspector"
            onClick={onClose}
            ref={closeRef}
          >
            ×
          </button>
        </header>
        <p id="evidence-drawer-summary" className="drawer-summary">
          {evidence.summary ?? "No evidence summary was provided."}
        </p>

        <dl className="evidence-meta">
          <div>
            <dt>Task</dt>
            <dd>{evidence.task_name ?? evidence.task_id}</dd>
          </div>
          <div>
            <dt>Solution</dt>
            <dd>
              {evidence.solution_name ??
                evidence.solution_id ??
                "Unspecified"}
            </dd>
          </div>
          <div>
            <dt>Artifact</dt>
            <dd>{humanBytes(evidence.byte_size)}</dd>
          </div>
        </dl>

        <div className="artifact-view">
          {contentState === "loading" && (
            <p role="status">Loading immutable artifact…</p>
          )}
          {contentState === "error" && (
            <p role="alert">
              The artifact preview could not be loaded. Download the original
              to inspect it.
            </p>
          )}
          {evidence.modality === "text" && (
            <pre>
              {previewContent === undefined
                ? "No text content."
                : structuredPreview
                  ? JSON.stringify(previewContent, null, 2)
                  : String(previewContent)}
            </pre>
          )}
          {evidence.modality === "structured" && (
            <pre>
              {JSON.stringify(previewContent ?? {}, null, 2)}
            </pre>
          )}
          {evidence.modality === "image" &&
            downloadUrl &&
            !imageFailed && (
              <img
                src={downloadUrl}
                alt={evidence.summary || evidence.label}
                onError={() => setImageFailed(true)}
              />
            )}
          {!["text", "structured"].includes(evidence.modality) && (
            <div className="artifact-fallback">
              <span aria-hidden="true">
                {evidence.modality === "image" ? "▧" : "⬇"}
              </span>
              <p>
                {modalityDescriptions[evidence.modality] ??
                  modalityDescriptions.unknown}
              </p>
            </div>
          )}
        </div>
        {downloadUrl ? (
          <a className="download-link" href={downloadUrl} download>
            Download original
            <span>{previewMediaType ?? "Unknown type"}</span>
          </a>
        ) : (
          <p className="unavailable-note">
            No downloadable artifact is attached to this evidence record.
          </p>
        )}
      </div>
    </div>
  );
}
