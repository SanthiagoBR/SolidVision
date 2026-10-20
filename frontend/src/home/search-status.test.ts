import { describe, expect, it } from "vitest";

import type { SearchState } from "../api/health";
import { LOADING_TEXT, searchStatusText, UNAVAILABLE_TEXT } from "./search-status";

describe("searchStatusText", () => {
  it.each<[SearchState, string | null, string | null]>([
    ["loading", null, LOADING_TEXT],
    [
      "unavailable",
      "The models are not in C:\\nowhere. Reinstall SolidVision.",
      "The models are not in C:\\nowhere. Reinstall SolidVision.",
    ],
    ["ready", null, null],
    ["on_demand", null, null],
  ])("for %s", (search, detail, text) => {
    expect(searchStatusText({ search, search_detail: detail })).toBe(text);
  });

  it("says search is unavailable when the API gave no line", () => {
    expect(searchStatusText({ search: "unavailable", search_detail: null })).toBe(
      UNAVAILABLE_TEXT,
    );
  });

  it("says nothing before /health answers", () => {
    expect(searchStatusText(undefined)).toBeNull();
  });
});
