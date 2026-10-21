import { describe, expect, it } from "vitest";

import {
  devicesText,
  formatCount,
  lastIndexedText,
  latestFinished,
  neverIndexedText,
  photosText,
  summarize,
  type DeviceCounts,
  type FinishedJob,
} from "./summary";

const active = {
  id: "00000000-0000-0000-0000-000000000001",
  status: "running" as const,
};

function device(
  label: string,
  indexed: number,
  job: DeviceCounts["active_job"] = null,
): DeviceCounts {
  return { id: `id-${label}`, label, indexed_images: indexed, active_job: job };
}

const finished = (at: string | null): FinishedJob => ({ finished_at: at });

describe("summarize", () => {
  it("counts the devices and sums what each has indexed", () => {
    const summary = summarize([device("HD1", 1_204), device("HD2", 37_208)], []);

    expect(summary.devices).toBe(2);
    expect(summary.indexedImages).toBe(38_412);
  });

  it("takes the greatest finished_at, and not the first of the list", () => {
    // Newest first by created_at: the first job was created last and finished
    // first, and the second, created before, finished after it.
    const jobs = [
      finished("2026-10-18T13:00:00+00:00"),
      finished("2026-10-18T22:10:00+00:00"),
      finished("2026-10-17T09:00:00Z"),
    ];

    expect(summarize([], jobs).lastIndexedAt?.toISOString()).toBe(
      "2026-10-18T22:10:00.000Z",
    );
  });

  it("compares instants, whatever offset each is written in", () => {
    expect(
      latestFinished([
        finished("2026-10-18T22:00:00-03:00"),
        finished("2026-10-19T00:30:00+00:00"),
      ])?.toISOString(),
    ).toBe("2026-10-19T01:00:00.000Z");
  });

  it("skips a job without a finished_at", () => {
    expect(latestFinished([finished(null)])).toBeNull();
  });

  it("names in the notice only the devices with none indexed and no job holding them", () => {
    const summary = summarize(
      [
        device("HD1", 1_204),
        device("HD3", 0, active),
        device("HD4", 0),
        device("HD5", 0),
      ],
      [],
    );

    expect(summary.neverIndexed.map((never) => never.label)).toEqual(["HD4", "HD5"]);
  });

  it("keeps the id of each, for Escolher pastas (RFC-039 section 5.11)", () => {
    const summary = summarize([device("HD1", 1_204), device("HD4", 0)], []);

    expect(summary.neverIndexed).toEqual([{ id: "id-HD4", label: "HD4" }]);
  });

  it("of nothing is nothing", () => {
    expect(summarize([], [])).toEqual({
      devices: 0,
      indexedImages: 0,
      lastIndexedAt: null,
      neverIndexed: [],
    });
  });
});

describe("the words", () => {
  it("writes counts as RFC-031 section 12 does", () => {
    expect(formatCount(1_204)).toBe("1.204");
    expect(formatCount(38_412)).toBe("38.412");
    expect(formatCount(0)).toBe("0");
  });

  it("says the devices and the photos in the singular and the plural", () => {
    expect(devicesText(0)).toBe("Nenhum dispositivo registrado");
    expect(devicesText(1)).toBe("1 dispositivo");
    expect(devicesText(4)).toBe("4 dispositivos");
    expect(photosText(1)).toBe("1 foto indexada");
    expect(photosText(0)).toBe("0 fotos indexadas");
    expect(photosText(38_412)).toBe("38.412 fotos indexadas");
  });

  it("dates the last indexing like the Figma, and adds the year of another year", () => {
    const now = new Date(2026, 9, 20, 12, 0);

    expect(lastIndexedText(new Date(2026, 8, 14, 22, 10), now)).toBe(
      "última indexação 14/09 às 22:10",
    );
    expect(lastIndexedText(new Date(2025, 11, 31, 8, 5), now)).toBe(
      "última indexação 31/12/2025 às 08:05",
    );
    expect(lastIndexedText(null, now)).toBe("nenhuma indexação ainda");
  });

  it("names one device in the notice, and how many more", () => {
    expect(neverIndexedText([])).toBeNull();
    expect(neverIndexedText(["HD4"])).toBe(
      "HD4 está registrado mas nunca foi indexado — suas fotos não aparecem em nenhuma busca.",
    );
    expect(neverIndexedText(["HD4", "HD5"])).toBe(
      "HD4 e mais 1 dispositivo estão registrados mas nunca foram indexados — suas fotos " +
        "não aparecem em nenhuma busca.",
    );
    expect(neverIndexedText(["HD4", "HD5", "HD6"])).toContain(
      "HD4 e mais 2 dispositivos",
    );
  });
});
