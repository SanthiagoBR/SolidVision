import { describe, expect, it } from "vitest";

import type { Folder, Job } from "../api/client";
import {
  cutLeft,
  devicePath,
  filterFolders,
  folderStateText,
  indexedText,
  parentOf,
  sortFolders,
  trail,
} from "./folders";
import { deselect, EMPTY, includedIn, isWithin, scopesOf, select } from "./selection";
import { OVERSCAN, ROW_HEIGHT, visibleRange } from "./window";

const NOW = new Date(2026, 9, 26, 15, 0);

function job(
  status: Job["status"],
  checkpoint: string | null = null,
  finished: string | null = null,
): Job {
  return {
    id: "00000000-0000-0000-0000-000000000039",
    device_id: "00000000-0000-0000-0000-0000000000d2",
    status,
    scopes: [],
    created_at: null,
    started_at: null,
    finished_at: finished,
    discovered_files: 0,
    discovery_complete: false,
    processed_images: 0,
    skipped_images: 0,
    failed_images: 0,
    last_processed_relative_path: checkpoint,
    cancel_requested: false,
    attempts: 0,
    error_message: null,
  };
}

function folder(
  state: Folder["state"],
  withJob: Job | null,
): Pick<Folder, "state" | "job"> {
  return { state, job: withJob };
}

describe("the text of each folder (section 5.6)", () => {
  const september = new Date(2026, 8, 14, 22, 10).toISOString();

  it.each([
    ["indexing", job("running"), "Indexando"],
    ["queued", job("pending"), "Na fila"],
    ["indexed", job("completed", null, september), "Indexada em 14/09"],
    [
      "partial",
      job("cancelled", null),
      "Parcial — cancelada antes do primeiro arquivo",
    ],
    ["partial", job("completed"), "Parcial — só parte das subpastas foi indexada"],
    ["partial", null, "Parcial"],
    ["never_indexed", null, "Nunca indexada"],
  ] as const)("%s with %o says %s", (state, withJob, text) => {
    expect(folderStateText(folder(state, withJob), NOW).text).toBe(text);
  });

  it("says where a cancelled job stopped, cut on the left, whole in the title", () => {
    const stopped = "fotos/2018/junho/IMG_2201.JPG";

    expect(folderStateText(folder("partial", job("cancelled", stopped)), NOW)).toEqual({
      text: "Parcial — a indexação parou em …/junho/IMG_2201.JPG",
      title: stopped,
    });
    expect(folderStateText(folder("partial", job("failed", stopped)), NOW).text).toBe(
      "Parcial — a indexação falhou em …/junho/IMG_2201.JPG",
    );
  });

  it("dates an indexing of today as today, and one of another year with it", () => {
    const today = new Date(2026, 9, 26, 9, 0).toISOString();
    const lastYear = new Date(2025, 6, 1).toISOString();

    expect(
      folderStateText(folder("indexed", job("completed", null, today)), NOW).text,
    ).toBe("Indexada hoje");
    expect(
      folderStateText(folder("indexed", job("completed", null, lastYear)), NOW).text,
    ).toBe("Indexada em 01/07/2025");
  });

  it("cuts a path on the left, and keeps a short one whole", () => {
    expect(cutLeft("2018/junho/IMG_2201.JPG")).toBe("…/junho/IMG_2201.JPG");
    expect(cutLeft("junho/IMG_2201.JPG")).toBe("junho/IMG_2201.JPG");
    expect(cutLeft("IMG_2201.JPG")).toBe("IMG_2201.JPG");
  });

  it("counts the indexed photos, and says nothing of zero", () => {
    expect(indexedText(1_204)).toBe("1.204 indexadas");
    expect(indexedText(1)).toBe("1 indexada");
    expect(indexedText(0)).toBeNull();
  });
});

describe("the order and the filter (section 2.7)", () => {
  const named = (...names: string[]) => names.map((name) => ({ name }));

  it("puts 2 before 10 and ignores case, as Explorer does", () => {
    expect(
      sortFolders(named("10", "Zebra", "2", "abacate", "1")).map((f) => f.name),
    ).toEqual(["1", "2", "10", "abacate", "Zebra"]);
  });

  it("filters case and accents aside", () => {
    const folders = named("São João 2019", "sao paulo", "Casamento", "JUNHO");

    expect(filterFolders(folders, "sao").map((f) => f.name)).toEqual([
      "São João 2019",
      "sao paulo",
    ]);
    expect(filterFolders(folders, "  junho ").map((f) => f.name)).toEqual(["JUNHO"]);
    expect(filterFolders(folders, "")).toBe(folders);
  });
});

describe("the URL of the folders screen (section 5.1)", () => {
  it("is the device, and the canonical path below the root", () => {
    expect(devicePath("d2")).toBe("/dispositivos/d2");
    expect(devicePath("d2", "")).toBe("/dispositivos/d2");
    expect(devicePath("d2", "2018/junho")).toBe("/dispositivos/d2?pasta=2018/junho");
  });

  it("encodes each part, and reads back the same path", () => {
    const href = devicePath("d2", "Fotos & Vídeos/50% #1?/a+b");
    const read = new URL(href, "http://127.0.0.1").searchParams.get("pasta");

    expect(href).toBe(
      "/dispositivos/d2?pasta=Fotos%20%26%20V%C3%ADdeos/50%25%20%231%3F/a%2Bb",
    );
    expect(read).toBe("Fotos & Vídeos/50% #1?/a+b");
  });
});

describe("the trail", () => {
  it("goes from the label to the folder, each part a level", () => {
    expect(trail("HD2", "2018/junho")).toEqual([
      { name: "HD2", path: "" },
      { name: "2018", path: "2018" },
      { name: "junho", path: "2018/junho" },
    ]);
    expect(trail("HD2", "")).toEqual([{ name: "HD2", path: "" }]);
    expect(parentOf("2018/junho")).toBe("2018");
    expect(parentOf("2018")).toBe("");
  });
});

describe("the selection (sections 2.6 and 5.6)", () => {
  it("compares paths by their parts", () => {
    expect(isWithin("2018/junho", "2018")).toBe(true);
    expect(isWithin("2018", "2018")).toBe(true);
    expect(isWithin("2018b/junho", "2018")).toBe(false);
    expect(isWithin("2018", "2018/junho")).toBe(false);
  });

  it("lets the parent absorb its children", () => {
    let selection = select(EMPTY, "2018/junho");
    selection = select(selection, "2018/julho");
    selection = select(selection, "2019");
    selection = select(selection, "2018");

    expect(scopesOf(selection)).toEqual(["2018", "2019"]);
  });

  it("leaves a folder inside a marked one out, as included", () => {
    const selection = select(EMPTY, "2018");

    expect(select(selection, "2018/junho")).toBe(selection);
    expect(includedIn(selection, "2018/junho/casamento")).toBe("2018");
    expect(includedIn(selection, "2018")).toBeNull();
    expect(includedIn(selection, "2018b")).toBeNull();
  });

  it("unmarks", () => {
    const selection = select(select(EMPTY, "2018"), "2019");

    expect(scopesOf(deselect(selection, "2018"))).toEqual(["2019"]);
    expect(deselect(selection, "2020")).toBe(selection);
  });

  it("sends the marked folders as the scopes, and never the root", () => {
    expect(scopesOf(select(EMPTY, ""))).toEqual([]);
    expect(scopesOf(select(select(EMPTY, "b"), "a/x"))).toEqual(["a/x", "b"]);
  });
});

describe("visibleRange (section 5.6)", () => {
  it("draws the visible rows and ten more on each side", () => {
    // 440 px of viewport: ten rows. Scrolled to row 100.
    const range = visibleRange(5_000, 100 * ROW_HEIGHT, 10 * ROW_HEIGHT);

    expect(range).toEqual({ start: 100 - OVERSCAN, end: 110 + OVERSCAN });
  });

  it("starts at the top", () => {
    expect(visibleRange(5_000, 0, 10 * ROW_HEIGHT)).toEqual({
      start: 0,
      end: 10 + OVERSCAN,
    });
  });

  it("stops at the end", () => {
    const range = visibleRange(5_000, 4_995 * ROW_HEIGHT, 10 * ROW_HEIGHT);

    expect(range).toEqual({ start: 4_995 - OVERSCAN, end: 5_000 });
  });

  it("is empty for no rows, and whole for a short list", () => {
    expect(visibleRange(0, 0, 600)).toEqual({ start: 0, end: 0 });
    expect(visibleRange(3, 0, 600)).toEqual({ start: 0, end: 3 });
  });

  it("draws one row even before the viewport is measured", () => {
    expect(visibleRange(5_000, 0, 0)).toEqual({ start: 0, end: 1 + OVERSCAN });
  });
});
