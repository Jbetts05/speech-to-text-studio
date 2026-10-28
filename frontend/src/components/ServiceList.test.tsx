import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { ServiceList } from "./ServiceList";
import { ConfigureRunWorkspace } from "./WorkspacePages";
import { mapBenchmark } from "../api/wire";
import { benchmarkWireFixture } from "../testing/fixtures";
import { serviceBenchmark, serviceNames, serviceRuntime } from "../testing/serviceFixtures";

describe("simple service lists", () => {
  it("uses pack-owned labels, retains stable identities and supports keyboard selection", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    function Selection() {
      const [ids, setIds] = useState(Object.keys(serviceNames));
      return <ServiceList solutions={serviceBenchmark.pipelines} runtime={serviceRuntime} selectedIds={ids} onSelectionChange={next => { change(next); setIds(next); }} />;
    }
    render(<Selection />);
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
    for (const name of Object.values(serviceNames)) expect(screen.getByRole("checkbox", { name })).toBeChecked();
    await user.tab();
    expect(screen.getByRole("checkbox", { name: "Azure Speech Fast" })).toHaveFocus();
    await user.keyboard(" ");
    expect(change).toHaveBeenLastCalledWith(Object.keys(serviceNames).slice(1));
    await user.tab();
    await user.keyboard(" ");
    expect(change).toHaveBeenLastCalledWith(Object.keys(serviceNames).slice(2));
    expect(screen.queryByText(/standalone|networked|comparable|adapter|compatibility/i)).not.toBeInTheDocument();
    const result = await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
    expect(result.violations).toEqual([]);
  });

  it("browses all services without selection controls, duplicate titles or routine badges", () => {
    render(<ServiceList solutions={serviceBenchmark.pipelines} runtime={serviceRuntime} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    for (const name of Object.values(serviceNames)) expect(screen.getAllByText(name)).toHaveLength(1);
    expect(screen.queryByText(/standalone|networked|comparable|adapter|compatibility/i)).not.toBeInTheDocument();
  });

  it("retains exact legacy IDs rather than guessing names or versions", () => {
    const legacy = mapBenchmark(benchmarkWireFixture);
    expect(legacy.pipelines?.map(solution => solution.name)).toEqual(["pipeline-a", "pipeline-b"]);
  });

  it("keeps unsupported and unknown services visible with specific reasons", () => {
    const solutions = serviceBenchmark.pipelines!;
    render(<ServiceList
      runtime={serviceRuntime}
      solutions={[
        { ...solutions[0], compatibility: { level: "unsupported", comparable_quality: false, reason: "This service cannot accept the dataset's audio format." } },
        { ...solutions[1], compatibility: undefined },
        { ...solutions[2], compatibility: { level: "partial-reference", comparable_quality: false, reason: "Only part of the reference can be evaluated." } },
      ]}
      selectedIds={[]}
      onSelectionChange={vi.fn()}
    />);
    expect(screen.getByRole("checkbox", { name: "Azure Speech Fast" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Azure Speech Fast" })).toHaveAccessibleDescription(/cannot accept/);
    expect(screen.getByRole("checkbox", { name: "GPT-Transcribe" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "GPT-Transcribe" })).toHaveAccessibleDescription(/not been confirmed/);
    expect(screen.getByRole("checkbox", { name: "Managed Whisper" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Managed Whisper" })).toHaveAccessibleDescription(/Only part/);
  });

  it("explains a missing catalog without inventing options", () => {
    render(<ServiceList runtime={serviceRuntime} />);
    expect(screen.getByRole("status")).toHaveTextContent("No services are available");
  });

  it.each([
    { runtime: undefined, message: /runtime settings/ },
    { runtime: { ...serviceRuntime, allow_networked: false }, message: /operator has disabled/ },
    { runtime: { ...serviceRuntime, worker_running: false }, message: /worker is not ready/ },
  ])("explains runtime service unavailability without removing options", ({ runtime, message }) => {
    render(<ServiceList solutions={serviceBenchmark.pipelines} runtime={runtime} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getAllByText(message)).toHaveLength(5);
  });

  it("blocks a selected service with missing compatibility and uses a quiet hosted cost note", () => {
    render(<ConfigureRunWorkspace
      benchmark={{ ...serviceBenchmark, pipelines: [{ ...serviceBenchmark.pipelines![0], compatibility: undefined }] }}
      solutionIds={["azure-speech-fast"]} retryLimit={2} allowNetworked launching={false}
      runtime={serviceRuntime} onSolutionIdsChange={vi.fn()} onRetryLimitChange={vi.fn()}
      onAllowNetworkedChange={vi.fn()} onLaunch={vi.fn()}
    />);
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Azure Speech Fast" })).toBeDisabled();
    expect(screen.getByText(/Launch authorizes live service calls/)).toHaveClass("service-usage-note");
    expect(screen.queryByLabelText(/Allow networked/)).not.toBeInTheDocument();
  });
});
