export const artifactExpiredEvent = "ai-bench:artifact-expired";

export function assertArtifactResponse(response: Response, url: string) {
  if (response.status === 410) {
    window.dispatchEvent(new CustomEvent(artifactExpiredEvent, { detail: { url } }));
    throw new Error("This artifact has expired.");
  }

  if (!response.ok) throw new Error(`Artifact request returned ${response.status}`);
}

export async function checkMediaExpiry(url: string) {
  try {
    const response = await fetch(url, { method: "HEAD" });
    if (response.status === 410) window.dispatchEvent(new CustomEvent(artifactExpiredEvent, { detail: { url } }));
  } catch { /* Native controls already expose media failure; never retry the media automatically. */ }
}
