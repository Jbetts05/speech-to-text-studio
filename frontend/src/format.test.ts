import { describe, expect, it } from "vitest";
import { formatMetricValue } from "./format";

describe("formatMetricValue", () => {
  it("formats recognized non-USD currencies without relabeling them as dollars", () => {
    expect(
      formatMetricValue({
        metric_id: "run_cost",
        family: "cost",
        value: 12.5,
        unit: "EUR",
      }),
    ).toBe("€12.5000");
  });

  it("keeps unknown currency units explicit", () => {
    expect(
      formatMetricValue({
        metric_id: "run_cost",
        family: "cost",
        value: 12.5,
        unit: "TOK",
      }),
    ).toMatch(/TOK.*12\.5000|12\.5000.*TOK/);
  });
});
