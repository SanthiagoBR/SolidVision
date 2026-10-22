import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MapReport } from "../shell/bridge";
import { finishMapGesture, startMapGesture } from "./timing";

let reports: MapReport[];

beforeEach(() => {
  reports = [];
  vi.stubGlobal("window", {
    solidvision: { reportMap: (r: MapReport) => reports.push(r) },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const painted = (receivedAt: number) => ({
  cells: 24,
  precisionRequested: 1,
  precisionApplied: 0,
  receivedAt,
});

describe("the mark of the map (RFC-040 section 5.6)", () => {
  it("goes from the gesture to the response and to the frame after the cells", () => {
    startMapGesture();
    const startedAt = performance.now();
    finishMapGesture(painted(startedAt + 100), startedAt + 150);

    expect(reports).toHaveLength(1);
    const [report] = reports;
    expect(report?.cells).toBe(24);
    expect(report?.precisionRequested).toBe(1);
    expect(report?.precisionApplied).toBe(0);
    expect(report?.responseMs).toBeGreaterThan(90);
    expect(report?.paintedMs).toBeGreaterThan(report?.responseMs ?? 0);
    expect(Object.keys(report ?? {}).sort()).toEqual([
      "cells",
      "paintedMs",
      "precisionApplied",
      "precisionRequested",
      "responseMs",
    ]);
  });

  it("is not written for cells from before the gesture, and the gesture ends", () => {
    startMapGesture();
    finishMapGesture(painted(performance.now() - 1_000), performance.now());
    finishMapGesture(painted(performance.now() + 50), performance.now() + 60);

    expect(reports).toEqual([]);
  });

  it("is written once per gesture", () => {
    startMapGesture();
    const now = performance.now();
    finishMapGesture(painted(now + 10), now + 20);
    finishMapGesture(painted(now + 30), now + 40);

    expect(reports).toHaveLength(1);
  });
});
