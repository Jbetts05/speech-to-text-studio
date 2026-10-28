import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Evidence, Modality } from "../types";
import { ArtifactPreview } from "./ArtifactPreview";

function evidence(modality: Modality, mediaType: string): Evidence {
  return {
    id: `evidence-${modality}`,
    task_id: "task-1",
    modality,
    label: `${modality} artifact`,
    artifact_sha256: "a".repeat(64),
    media_type: mediaType,
    role: "input",
    source: "result",
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ArtifactPreview", () => {
  it("renders fetched text and structured artifacts inline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify({ value: "evidence" }), {
          headers: { "Content-Type": "application/json" },
        })),
      ),
    );
    const { rerender } = render(
      <ArtifactPreview
        evidence={evidence("text", "application/json")}
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );
    expect(await screen.findByText(/"value": "evidence"/)).toBeInTheDocument();

    rerender(
      <ArtifactPreview
        evidence={evidence("structured", "application/json")}
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );
    expect(await screen.findByText(/"value": "evidence"/)).toBeInTheDocument();
  });

  it("keeps loading an immutable artifact across evidence object refreshes", async () => {
    let resolveFetch!: (response: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const initialEvidence = evidence("structured", "application/json");
    const { rerender } = render(
      <ArtifactPreview
        evidence={initialEvidence}
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );
    expect(await screen.findByText("Loading evidence…")).toBeInTheDocument();

    rerender(
      <ArtifactPreview
        evidence={{ ...initialEvidence, summary: "Refreshed run snapshot" }}
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveFetch(
      new Response(JSON.stringify({ value: "stable" }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    expect(await screen.findByText(/"value": "stable"/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["image", "image/png", "img"],
    ["audio", "audio/wav", "audio"],
    ["video", "video/mp4", "video"],
  ] as const)("uses the %s modality renderer", async (modality, mediaType, selector) => {
    const { container } = render(
      <ArtifactPreview
        evidence={evidence(modality, mediaType)}
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );
    await waitFor(() => expect(container.querySelector(selector)).not.toBeNull());
    expect(
      screen.getByRole("button", { name: "Inspect artifact" }),
    ).toBeInTheDocument();
  });

  it("does not embed attachment-only document artifacts", async () => {
    const { container } = render(
      <ArtifactPreview
        evidence={evidence("document", "application/pdf")}
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );

    expect(
      await screen.findByText("Document preview unavailable"),
    ).toBeInTheDocument();
    expect(container.querySelector("iframe")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Inspect artifact" }),
    ).toBeInTheDocument();
  });

  it("keeps an explicit empty state when no evidence is selected", () => {
    render(
      <ArtifactPreview
        artifactUrl={(sha) => `/api/artifacts/${sha}`}
        onInspect={() => undefined}
      />,
    );
    expect(screen.getByText("No artifact selected")).toBeInTheDocument();
  });
});
