import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EvidenceDrawer } from "./EvidenceDrawer";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EvidenceDrawer", () => {
  it("loads immutable text artifact content for preview", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("retrieved evidence", {
          headers: {
            "Content-Type": "text/plain",
            "Content-Length": "18",
          },
        }),
      ),
    );

    render(
      <EvidenceDrawer
        evidence={{
          id: "evidence-1",
          task_id: "task-1",
          modality: "text",
          label: "Raw output",
          artifact_sha256: "a".repeat(64),
          media_type: "text/plain",
          role: "raw",
          source: "result",
        }}
        artifactUrl={(id) => `/api/artifacts/${id}`}
        onClose={() => undefined}
      />,
    );

    expect(await screen.findByText("retrieved evidence")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      `/api/artifacts/${"a".repeat(64)}`,
      expect.objectContaining({
        headers: { Accept: "text/plain" },
      }),
    );
  });

  it("renders JSON responses as structured content even when the pack viewer is text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ predicted: "archive" }), {
          headers: {
            "Content-Type": "application/json",
          },
        }),
      ),
    );

    render(
      <EvidenceDrawer
        evidence={{
          id: "evidence-2",
          task_id: "task-2",
          modality: "text",
          label: "Raw provider output",
          artifact_sha256: "b".repeat(64),
          media_type: "text/plain",
          role: "raw",
          source: "result",
        }}
        artifactUrl={(id) => `/api/artifacts/${id}`}
        onClose={() => undefined}
      />,
    );

    expect(await screen.findByText(/"predicted": "archive"/)).toBeInTheDocument();
    expect(screen.getByText("application/json")).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });
});
