import type { ApiClient } from "../api/client";
import type { DatasetImport, UploadRequest, UploadSession } from "../types";

export type UploadIdentity = {
  key: string;
  name: string;
  uploadId?: string;
  fingerprint?: string;
  finalizeKey?: string;
};

const storageKey = (benchmarkId: string) => `ai-bench.upload.${benchmarkId}`;

export function readUploadIdentity(benchmarkId: string): UploadIdentity | undefined {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey(benchmarkId)) ?? "null") as UploadIdentity | null;
    if (value && typeof value.key === "string" && typeof value.name === "string") return value;
  } catch { /* A blocked or cleared session store must not break catalog reads. */ }
  return undefined;
}

export function saveUploadIdentity(benchmarkId: string, identity?: UploadIdentity) {
  if (identity) sessionStorage.setItem(storageKey(benchmarkId), JSON.stringify(identity));
  else sessionStorage.removeItem(storageKey(benchmarkId));
}

function normalizedName(name: string) {
  return name.normalize("NFKC").toUpperCase().toLowerCase();
}

export function validateUploadFiles(files: File[], description: DatasetImport): void {
  if (!files.length || files.length > description.max_files) {
    throw new Error(`Select 1–${Math.floor(description.max_files / 2)} exact input/reference basename pairs (at most ${description.max_files} files).`);
  }
  const names = new Set<string>();
  const basenames = new Map<string, string>();
  const pairs = new Map<string, Set<string>>();
  let total = 0;
  for (const file of files) {
    const name = file.name;
    if (
      !name || /[\\/:\u0000-\u001f\u007f]/u.test(name) ||
      name.startsWith(".") || name.endsWith(".") || name.endsWith(" ") ||
      file.webkitRelativePath
    ) {
      throw new Error("Select individual files with plain filenames, not folders, hidden files or nested paths.");
    }
    const normalized = normalizedName(name);
    if (names.has(normalized)) throw new Error(`Duplicate, case or Unicode filename collision: ${name}. Rename files rather than guessing a match.`);
    names.add(normalized);
    const dot = name.lastIndexOf(".");
    const extension = name.slice(dot).toLowerCase();
    const stem = name.slice(0, dot);
    const role = description.input_extensions.includes(extension)
      ? "input"
      : description.reference_extensions.includes(extension) ? "reference" : undefined;
    if (dot <= 0 || !role) throw new Error(`Unsupported file: ${name}. Select ${[...description.input_extensions, ...description.reference_extensions].join(", ")} files.`);
    const previous = basenames.get(normalizedName(stem));
    if (previous && previous !== stem) throw new Error(`Basenames must match exactly, including case and Unicode: ${previous} / ${stem}.`);
    basenames.set(normalizedName(stem), stem);
    const pair = pairs.get(stem) ?? new Set<string>();
    if (pair.has(role)) throw new Error(`Select exactly one input and one reference for ${stem}.`);
    pair.add(role);
    pairs.set(stem, pair);
    const limit = Math.min(role === "input" ? description.max_input_bytes : description.max_reference_bytes, 25_000_000);
    if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > limit) {
      throw new Error(`${name} must be nonempty and at most ${limit.toLocaleString("en-US")} bytes.`);
    }
    total += file.size;
    if (total > description.max_total_bytes) throw new Error(`Selected files exceed ${description.max_total_bytes.toLocaleString("en-US")} bytes in total.`);
  }
  for (const [stem, roles] of pairs) {
    if (roles.size !== 2) throw new Error(`Missing exact input/reference pair for ${stem}. Match basenames exactly.`);
  }
}

export async function hashUploadFiles(
  files: File[],
  signal: AbortSignal,
  onProgress: (message: string) => void,
): Promise<UploadRequest["files"]> {
  const parts: UploadRequest["files"] = [];
  // One bounded file buffer at a time; never assemble the dataset in memory.
  for (const file of [...files].sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
    signal.throwIfAborted();
    onProgress(`Checking SHA-256: ${file.name}`);
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    signal.throwIfAborted();
    parts.push({
      name: file.name,
      size_bytes: file.size,
      sha256: Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""),
    });
  }
  return parts;
}

function verifyReservation(session: UploadSession, request: UploadRequest, uploadId?: string) {
  if ((uploadId !== undefined && session.upload_id !== uploadId) ||
      session.benchmark_id !== request.benchmark_id || session.name !== request.name ||
      session.parts.length !== request.files.length ||
      new Set(session.parts.map((part) => part.name)).size !== session.parts.length) {
    throw new Error("Upload reservation does not match this dataset. Do not send different files to an existing session.");
  }
  for (const part of session.parts) {
    const file = request.files.find((item) => item.name === part.name);
    if (!file || file.size_bytes !== part.size_bytes || file.sha256 !== part.sha256) {
      throw new Error(`Reselect the original reserved file with the same name, size and SHA-256: ${part.name}.`);
    }
  }
  if (session.status === "failed" || session.status === "expired") {
    throw new Error(session.error ?? `Upload ${session.status}. Start a new upload with valid files.`);
  }
  if (["ready", "validating"].includes(session.status) && session.parts.some((part) => part.status !== "complete")) {
    throw new Error("The server has not confirmed every reserved part. Check status; publication is not confirmed.");
  }
}

export async function transmitUpload({
  api, description, benchmarkId, name, files, identity, signal, onIdentity, onSession, onProgress,
}: {
  api: ApiClient;
  description: DatasetImport;
  benchmarkId: string;
  name: string;
  files: File[];
  identity?: UploadIdentity;
  signal: AbortSignal;
  onIdentity: (identity: UploadIdentity) => void;
  onSession: (session: UploadSession) => void;
  onProgress: (message: string) => void;
}): Promise<UploadSession> {
  if (!api.createUpload || !api.getUpload || !api.uploadPart || !api.finalizeUpload) {
    throw new Error("Dataset import is unavailable from this API.");
  }
  validateUploadFiles(files, description);
  const request: UploadRequest = {
    benchmark_id: benchmarkId, name: name.trim(), language: description.language,
    public_acknowledged: true, rights_acknowledged: true, language_acknowledged: true,
    files: await hashUploadFiles(files, signal, onProgress),
  };
  if (!request.name) throw new Error("Enter a dataset name.");
  const encoded = JSON.stringify(request);
  if (new TextEncoder().encode(encoded).byteLength > 32_768) throw new Error("Reservation exceeds 32 KiB. Shorten the dataset and file names.");
  const fingerprint = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(encoded))),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  if (identity?.fingerprint && identity.fingerprint !== fingerprint) {
    throw new Error("Reselect the original files and dataset name. Their reservation differs from the pending upload.");
  }
  let current = identity ?? { key: api.createOperationKey?.() ?? crypto.randomUUID(), name: request.name };
  current = { ...current, fingerprint };
  onIdentity(current);
  signal.throwIfAborted();
  let session = current.uploadId
    ? await api.getUpload(current.uploadId, signal)
    : await api.createUpload(request, current.key, signal);
  verifyReservation(session, request, current.uploadId);
  current = { ...current, uploadId: session.upload_id };
  onIdentity(current);
  onSession(session);
  for (const file of files) {
    signal.throwIfAborted();
    if (session.status !== "receiving") break;
    const part = session.parts.find((item) => item.name === file.name)!;
    if (part.status === "complete") continue;
    onProgress(`Sending ${file.name} (${session.parts.filter((item) => item.status === "complete").length}/${files.length} files complete)`);
    session = await api.uploadPart(session.upload_id, part.part_id, file, signal);
    verifyReservation(session, request, current.uploadId);
    onSession(session);
    if (session.parts.find((item) => item.part_id === part.part_id)?.status !== "complete") {
      throw new Error("The server has not confirmed this file. Check status and resume before sending more files.");
    }
  }
  signal.throwIfAborted();
  if (session.status === "receiving") {
    if (session.parts.some((part) => part.status !== "complete")) throw new Error("Not all reserved files are complete. Check status and resume.");
    current = { ...current, finalizeKey: current.finalizeKey ?? api.createOperationKey?.() ?? crypto.randomUUID() };
    onIdentity(current);
    onProgress("All files received. Requesting server validation…");
    session = await api.finalizeUpload(session.upload_id, current.finalizeKey!, signal);
    verifyReservation(session, request, current.uploadId);
    onSession(session);
  }
  return session;
}
