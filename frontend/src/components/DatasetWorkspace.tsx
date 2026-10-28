import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { BenchmarkManifest, Dataset, Evidence, UploadSession } from "../types";
import {
  readUploadIdentity, saveUploadIdentity, transmitUpload, validateUploadFiles,
  type UploadIdentity,
} from "../state/datasetUpload";
import { ArtifactPreview } from "./ArtifactPreview";

export function RetentionNotice({ pinned, expires_at }: { pinned?: boolean; expires_at?: string | null }) {
  if (pinned) return <span className="retention-notice">Pinned · no expiry</span>;
  if (!expires_at) return null;
  return <span className="retention-notice">Expires <time dateTime={expires_at}>{new Date(expires_at).toLocaleString()}</time> · fixed deadline</span>;
}

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : "The request failed. Check status before retrying.";
}

function DatasetArtifacts({
  dataset, benchmark, api, onInspect,
}: {
  dataset: Dataset;
  benchmark: BenchmarkManifest;
  api: ApiClient;
  onInspect: (evidence: Evidence, trigger: HTMLButtonElement) => void;
}) {
  const samples = Object.keys(dataset.sample_artifacts ?? {});
  const [sampleId, setSampleId] = useState(samples[0] ?? "");
  const selectedSampleId = samples.includes(sampleId) ? sampleId : samples[0];
  const artifacts = selectedSampleId ? dataset.sample_artifacts?.[selectedSampleId] ?? [] : [];
  if (!samples.length) return <p>Sample artifacts are not available for this revision.</p>;

  return (
    <section aria-label="Dataset sample evidence">
      <label className="dataset-selection">
        <span>Dataset sample</span>
        <select value={selectedSampleId} onChange={(event) => setSampleId(event.target.value)}>
          {samples.map((sample) => <option key={sample} value={sample}>{sample}</option>)}
        </select>
      </label>
      <p>Preview or download the selected sample before launching a run. Only one sample is loaded at a time.</p>
      <div className="dataset-artifacts">
        {artifacts.map((artifact) => {
          const evidence: Evidence = {
            id: `${selectedSampleId}:${artifact.role}:${artifact.artifact_id}`, task_id: selectedSampleId,
            label: `${artifact.role} · ${selectedSampleId}`, source: "sample", role: artifact.role,
            modality: artifact.modality, artifact_sha256: artifact.artifact_id,
            media_type: artifact.media_type, byte_size: artifact.size_bytes,
          };
          return <div key={evidence.id}>
            <ArtifactPreview evidence={evidence} viewer={benchmark.viewer} artifactUrl={(id) => api.artifactUrl(id)} onInspect={onInspect} />
            <a className="download-link" href={api.artifactUrl(artifact.artifact_id)} download>Download {artifact.role} · {selectedSampleId}</a>
          </div>;
        })}
      </div>
    </section>
  );
}

export function DatasetWorkspace({
  api, benchmark, selectedId, selected, selectionError, onSelect, onInspect, onRefreshSelected,
}: {
  api: ApiClient;
  benchmark: BenchmarkManifest;
  selectedId?: string;
  selected?: Dataset;
  selectionError?: string;
  onSelect: (id: string) => void;
  onInspect: (evidence: Evidence, trigger: HTMLButtonElement) => void;
  onRefreshSelected?: () => void;
}) {
  const description = benchmark.dataset_import!;
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [catalogError, setCatalogError] = useState<string>();
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [identity, setIdentity] = useState(() => readUploadIdentity(benchmark.id));
  const [name, setName] = useState(() => identity?.name ?? "");
  const [files, setFiles] = useState<File[]>([]);
  const [session, setSession] = useState<UploadSession>();
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(identity ? "Reselect original files to resume. Stored files are not available to the browser after reload." : "");
  const [error, setError] = useState<string>();
  const [expired, setExpired] = useState(false);
  const [acknowledgments, setAcknowledgments] = useState([false, false, false, false]);
  const controller = useRef<AbortController | undefined>(undefined);
  const mounted = useRef(true);
  const handledReady = useRef<string | undefined>(undefined);
  const selectWhenReady = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const restoredUpload = useRef<string | undefined>(undefined);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controller.current?.abort(); };
  }, []);

  const loadCatalog = useCallback(async (nextCursor?: string, signal?: AbortSignal) => {
    if (!api.listDatasets) return;
    setCatalogBusy(true);
    try {
      const page = await api.listDatasets(benchmark.id, nextCursor, signal);
      if (signal?.aborted || !mounted.current) return;
      setDatasets((current) => nextCursor ? [...current, ...page.items.filter((item) => !current.some((existing) => existing.id === item.id))] : page.items);
      setCursor(page.next_cursor ?? undefined);
      setCatalogError(undefined);
    } catch (reason) {
      if (!signal?.aborted && mounted.current) setCatalogError(message(reason));
    } finally {
      if (!signal?.aborted && mounted.current) setCatalogBusy(false);
    }
  }, [api, benchmark.id]);

  useEffect(() => {
    const abort = new AbortController();
    void loadCatalog(undefined, abort.signal);
    return () => abort.abort();
  }, [loadCatalog]);

  const checkStatus = useCallback(async (id: string, signal?: AbortSignal) => {
    try {
      const next = await api.getUpload!(id, signal);
      if (signal?.aborted || !mounted.current) return;
      restoredUpload.current = id;
      setSession(next);
      setError(next.status === "failed" ? next.error ?? "Dataset validation failed." : undefined);
      setExpired(next.status === "expired");
      setStatus(next.status === "receiving"
        ? "Reception paused. Reselect the original files and resume; completed files will not be resent."
        : next.status === "validating" ? "Server validation in progress. This is not yet a published dataset."
        : next.status === "ready" ? "Dataset published." : `Upload ${next.status}.`);
    } catch (reason) {
      if (signal?.aborted || !mounted.current) return;
      setError(message(reason));
      if (reason instanceof ApiError && reason.status === 410) setExpired(true);
    }
  }, [api]);

  useEffect(() => {
    if (!identity?.uploadId || !api.getUpload || expired || busy || restoredUpload.current === identity.uploadId) return;
    const abort = new AbortController();
    void checkStatus(identity.uploadId, abort.signal);
    return () => abort.abort();
  }, [identity?.uploadId, api, checkStatus, expired, busy]);

  useEffect(() => {
    if (!session || expired || busy) return;
    if (session.status === "ready") {
      if (!session.dataset_id) {
        setError("The server reported ready without a published dataset identity. Check status; publication is not confirmed.");
      } else if (selectWhenReady.current && handledReady.current !== session.dataset_id) {
        handledReady.current = session.dataset_id;
        onSelectRef.current(session.dataset_id);
        void loadCatalog();
      }
      return;
    }
    if (session.status !== "validating" || error) return;
    const abort = new AbortController();
    const timer = window.setTimeout(() => void checkStatus(session.upload_id, abort.signal), 1500);
    return () => { window.clearTimeout(timer); abort.abort(); };
  }, [session, expired, busy, error, checkStatus, loadCatalog]);

  const retainIdentity = (next: UploadIdentity) => {
    // Persist before the request: a lost create response must reuse its reservation key.
    saveUploadIdentity(benchmark.id, next);
    if (next.uploadId) restoredUpload.current = next.uploadId;
    setIdentity(next);
  };

  const start = async () => {
    if (busy || !acknowledgments.every(Boolean)) return;
    const abort = new AbortController();
    controller.current = abort;
    selectWhenReady.current = true;
    setBusy(true);
    setError(undefined);
    try {
      const next = await transmitUpload({
        api, description, benchmarkId: benchmark.id, name, files, identity,
        signal: abort.signal, onIdentity: retainIdentity,
        onSession: (value) => { if (mounted.current) setSession(value); },
        onProgress: (value) => { if (mounted.current) setStatus(value); },
      });
      if (mounted.current) setStatus(next.status === "ready" ? "Dataset published." : "Server validation in progress. Not yet published.");
    } catch (reason) {
      if (!mounted.current) return;
      if (abort.signal.aborted) setStatus("Transmission paused, not deleted. Check status and reselect original files to resume.");
      else {
        setError(message(reason));
        if (reason instanceof ApiError && reason.status === 410) setExpired(true);
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const language = new Intl.DisplayNames(["en"], { type: "language" }).of(description.language) ?? description.language;
  const warnings = [
    "PUBLIC: anyone can view, play and download this dataset and its derived results.",
    "I have the rights to share these files publicly. They contain no sensitive or personal content.",
    `These files satisfy the ${language}-only language and evaluation requirements.`,
    "I understand uploads and everything derived from them expire ten days after publication. Viewing, retries and new runs do not extend expiry. Downloaded copies and provider-side retention cannot be recalled.",
  ];
  const terminal = expired || session?.status === "failed" || session?.status === "ready";
  return (
    <section className="dataset-workspace home-panel" aria-labelledby="dataset-heading">
      <div className="home-panel-heading">
        <div><span className="section-kicker">Immutable revisions</span><h2 id="dataset-heading">Datasets</h2></div>
        <button type="button" className="secondary-button" disabled={catalogBusy} onClick={() => {
          void loadCatalog();
          onRefreshSelected?.();
        }}>Refresh datasets</button>
      </div>
      <label className="dataset-selection">
        <span>Dataset for the next run</span>
        <select value={selectedId ?? ""} onChange={(event) => onSelect(event.target.value)}>
          <option value="">Installed default · {benchmark.scored_sample_count} scored samples</option>
          {selectedId && !datasets.some((item) => item.id === selectedId) && <option value={selectedId}>{selected?.name ?? selectedId}</option>}
          {datasets.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.scored_sample_count} scored samples{item.pinned ? " · pinned" : item.expires_at ? ` · expires ${new Date(item.expires_at).toLocaleDateString()}` : ""}</option>)}
        </select>
      </label>
      {cursor && <button type="button" className="text-button" disabled={catalogBusy} onClick={() => void loadCatalog(cursor)}>Load more datasets</button>}
      {(selectionError || catalogError) && <p className="inline-error" role="alert">{selectionError ?? catalogError}</p>}
      {selected && !selectionError && (
        <div className="dataset-details">
          <p><strong>{selected.name}</strong> · {selected.sample_count} samples · {selected.scored_sample_count} scored samples</p>
          <RetentionNotice {...selected} />
          <p>Revision <code>{selected.revision.id}@{selected.revision.version}</code>. Selecting it does not change any historical run.</p>
          <DatasetArtifacts key={selected.id} dataset={selected} benchmark={benchmark} api={api} onInspect={onInspect} />
        </div>
      )}
      <details className="dataset-import" open={Boolean(identity)}>
        <summary>{description.title}</summary>
        <p>Choose original files directly. No folders, archives, URLs or microphone capture. Matching basenames must be exact.</p>
        <ul>{description.instructions.map((instruction, index) => <li key={index}>{instruction}</li>)}</ul>
        <p>At most {description.max_files} files ({Math.floor(description.max_files / 2)} pairs). Input: {description.max_input_bytes.toLocaleString("en-US")} bytes each; reference: {description.max_reference_bytes.toLocaleString("en-US")} bytes each; total: {description.max_total_bytes.toLocaleString("en-US")} bytes. The server validates content and duration before publication.</p>
        <form onSubmit={(event) => { event.preventDefault(); void start(); }}>
          <label className="dataset-selection"><span>Dataset name</span><input value={name} maxLength={160} required disabled={busy || Boolean(identity)} onChange={(event) => setName(event.target.value)} /></label>
          <label className="dataset-selection"><span>Select input and reference files</span>
            <input ref={fileInput} type="file" multiple accept={[...description.input_extensions, ...description.reference_extensions].join(",")} disabled={busy || terminal} onChange={(event) => {
              const next = Array.from(event.target.files ?? []);
              setFiles(next);
              setError(undefined);
              try { validateUploadFiles(next, description); } catch (reason) { setError(message(reason)); }
            }} />
          </label>
          <p>{files.length} files selected. SHA-256 is checked one file at a time only after acknowledgment.</p>
          <fieldset disabled={busy || terminal} className="dataset-acknowledgments">
            <legend>Required before any transmission</legend>
            {warnings.map((warning, index) => <label key={warning}><input type="checkbox" checked={acknowledgments[index]} onChange={(event) => setAcknowledgments((current) => current.map((value, i) => i === index ? event.target.checked : value))} /><span>{warning}</span></label>)}
          </fieldset>
          <div className="builder-actions">
            <button type="submit" className="primary-button" disabled={busy || terminal || session?.status === "validating" || !name.trim() || !files.length || !acknowledgments.every(Boolean)}>{busy ? "Transmitting…" : identity ? "Resume upload" : "Upload public dataset"}</button>
            {busy && <button type="button" className="secondary-button" onClick={() => controller.current?.abort()}>Pause transmission</button>}
            {identity?.uploadId && !busy && !expired && <button type="button" className="secondary-button" onClick={() => void checkStatus(identity.uploadId!)}>Check upload status</button>}
            {session?.status === "ready" && session.dataset_id && <button type="button" className="secondary-button" onClick={() => onSelect(session.dataset_id!)}>Select published dataset</button>}
            {identity && !busy && <button type="button" className="text-button" onClick={() => {
              saveUploadIdentity(benchmark.id);
              if (fileInput.current) fileInput.current.value = "";
              selectWhenReady.current = false;
              setIdentity(undefined); setSession(undefined); setFiles([]); setName("");
              setExpired(false); setError(undefined); setAcknowledgments([false, false, false, false]);
              setStatus("Local resume link cleared, not deleted. Incomplete sessions expire on the server; published data keeps its fixed deadline.");
            }}>Forget resume link (not delete)</button>}
          </div>
          <p role="status" aria-live="polite">{expired ? "Upload expired. It cannot be resumed." : status}</p>
          {session && <p>Session {session.upload_id} · {session.parts.filter((part) => part.status === "complete").length}/{session.parts.length} files confirmed · session deadline <time dateTime={session.expires_at}>{new Date(session.expires_at).toLocaleString()}</time></p>}
          {error && <p className="inline-error" role="alert">{error}</p>}
        </form>
      </details>
    </section>
  );
}
