import { describe, expect, it } from "vitest";

import {
  dateFilterText,
  formatCapturedAt,
  formatDate,
  isInverted,
  isIsoDate,
  nextDay,
} from "./dates";

describe("nextDay", () => {
  it("adds one day on the numbers of the date", () => {
    expect(nextDay("2018-06-14")).toBe("2018-06-15");
    expect(nextDay("2018-06-30")).toBe("2018-07-01");
  });

  it("crosses the end of the year", () => {
    expect(nextDay("2018-12-31")).toBe("2019-01-01");
  });

  it("knows the end of February, leap year or not", () => {
    expect(nextDay("2020-02-28")).toBe("2020-02-29");
    expect(nextDay("2020-02-29")).toBe("2020-03-01");
    expect(nextDay("2019-02-28")).toBe("2019-03-01");
    expect(nextDay("1900-02-28")).toBe("1900-03-01");
    expect(nextDay("2000-02-28")).toBe("2000-02-29");
  });

  it("refuses what is not a day", () => {
    expect(() => nextDay("2019-02-29")).toThrow();
    expect(() => nextDay("2018-12-31T00:00:00Z")).toThrow();
  });
});

describe("isIsoDate", () => {
  it("is AAAA-MM-DD of a day that exists, and nothing else", () => {
    expect(isIsoDate("2018-12-31")).toBe(true);
    expect(isIsoDate("2018-13-01")).toBe(false);
    expect(isIsoDate("2018-02-30")).toBe(false);
    expect(isIsoDate("31/12/2018")).toBe(false);
    expect(isIsoDate("2018-12-31T00:00")).toBe(false);
    expect(isIsoDate("")).toBe(false);
  });
});

describe("the words", () => {
  it("formats a day as the screen writes it", () => {
    expect(formatDate("2018-01-01")).toBe("01/01/2018");
  });

  it("formats captured_at from its text, with no time zone in between", () => {
    expect(formatCapturedAt("2018-07-14T15:32:05")).toBe("14/07/2018 às 15:32");
    // A time that falls in a daylight-saving jump somewhere stays the text's.
    expect(formatCapturedAt("2018-11-04T00:30:00")).toBe("04/11/2018 às 00:30");
  });

  it("says a range with both ends inclusive, as chosen", () => {
    expect(dateFilterText("2018-01-01", "2018-12-31")).toBe(
      "Capturadas de 01/01/2018 a 31/12/2018",
    );
    expect(dateFilterText("2018-01-01", null)).toBe(
      "Capturadas a partir de 01/01/2018",
    );
    expect(dateFilterText(null, "2018-12-31")).toBe("Capturadas até 31/12/2018");
    expect(dateFilterText(null, null)).toBeNull();
  });

  it("knows an inverted range", () => {
    expect(isInverted("2018-12-31", "2018-01-01")).toBe(true);
    expect(isInverted("2018-01-01", "2018-01-01")).toBe(false);
    expect(isInverted(null, "2018-01-01")).toBe(false);
  });
});
