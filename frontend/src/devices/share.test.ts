import { describe, expect, it } from "vitest";

import { diskBarText, scanShare, type ScanFacts } from "./share";
import { dayText, mountText, sizeText } from "./words";

const NOW = new Date(2026, 9, 26, 15, 0);
const MARCH = new Date(2026, 2, 12, 10, 30).toISOString();

function device(
  indexed: number,
  scanned: number | null,
  at: string | null = MARCH,
): ScanFacts {
  return { indexed_images: indexed, last_scan_file_count: scanned, last_scan_at: at };
}

describe("scanShare", () => {
  it("rounds down, so that 100% means every file", () => {
    expect(scanShare(device(24_104, 48_210), NOW)).toMatchObject({
      kind: "share",
      percent: 49,
      text: "49% no scan de 12/03",
    });
    expect(scanShare(device(48_209, 48_210), NOW)).toMatchObject({ percent: 99 });
    expect(scanShare(device(48_210, 48_210), NOW)).toMatchObject({
      percent: 100,
      text: "100% no scan de 12/03",
    });
  });

  it("says less than 1% between zero and one, and 0% at zero", () => {
    expect(scanShare(device(12, 48_210), NOW)).toMatchObject({
      kind: "share",
      percent: 0,
      text: "menos de 1% no scan de 12/03",
    });
    expect(scanShare(device(0, 48_210), NOW)).toMatchObject({
      percent: 0,
      text: "0% no scan de 12/03",
    });
  });

  it("says nothing without a denominator, or without its date", () => {
    expect(scanShare(device(1_204, null, null), NOW)).toEqual({ kind: "none" });
    expect(scanShare(device(1_204, 2_000, null), NOW)).toEqual({ kind: "none" });
    expect(scanShare(device(1_204, 2_000, "not a date"), NOW)).toEqual({
      kind: "none",
    });
  });

  it("says the scan found no photos, for a denominator of zero", () => {
    expect(scanShare(device(0, 0), NOW)).toEqual({
      kind: "empty",
      text: "o scan de 12/03 não achou fotos",
    });
  });

  it("shows no number above 100%: the disk changed since the scan", () => {
    expect(scanShare(device(48_300, 48_210), NOW)).toEqual({
      kind: "changed",
      text: "o disco mudou desde o scan de 12/03",
    });
  });

  it("dates the scan with the year of another year, and as today", () => {
    const lastYear = new Date(2025, 11, 31, 23, 0).toISOString();
    const today = new Date(2026, 9, 26, 9, 30).toISOString();

    expect(scanShare(device(1, 4, lastYear), NOW)).toMatchObject({
      text: "25% no scan de 31/12/2025",
    });
    expect(scanShare(device(1, 4, today), NOW)).toMatchObject({
      text: "25% no scan de hoje",
    });
  });
});

describe("diskBarText", () => {
  it("is the bar of the job panel, with both counts", () => {
    const today = new Date(2026, 9, 26, 9, 30).toISOString();

    expect(diskBarText(device(12_000, 48_210, today), NOW)).toBe(
      "24% do disco · 12.000 de 48.210 no scan de hoje",
    );
    expect(diskBarText(device(12_000, null, null), NOW)).toBeNull();
    expect(diskBarText(device(0, 0), NOW)).toBeNull();
  });
});

describe("the words", () => {
  it("dates a day", () => {
    expect(dayText(new Date(2026, 9, 26, 0, 1), NOW)).toBe("hoje");
    expect(dayText(new Date(2026, 8, 14), NOW)).toBe("14/09");
    expect(dayText(new Date(2024, 0, 2), NOW)).toBe("02/01/2024");
  });

  it("writes a mount point as Windows does", () => {
    expect(mountText("F:/")).toBe("F:");
    expect(mountText("C:/montagens/hd2/")).toBe("C:\\montagens\\hd2");
    expect(mountText(null)).toBe("");
  });

  it("writes the size of a disk as its box does", () => {
    expect(sizeText(2_000_398_934_016)).toBe("2 TB");
    expect(sizeText(500_107_862_016)).toBe("500 GB");
    expect(sizeText(31_914_983_424)).toBe("32 GB");
    expect(sizeText(7_864_320_000)).toBe("7,9 GB");
    expect(sizeText(1_500_301_910_016)).toBe("1,5 TB");
    expect(sizeText(null)).toBe("");
  });
});
