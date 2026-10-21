import { describe, expect, it } from "vitest";

import { PAGE_SIZE } from "./actions";
import { canShowMore, excludedText, summaryText } from "./SearchScreen";

describe("Mostrar mais", () => {
  it("appears when 40 of 40 came", () => {
    expect(canShowMore(40, PAGE_SIZE, 100)).toBe(true);
  });

  it("does not appear with 39 of 40, nor with 100", () => {
    expect(canShowMore(39, PAGE_SIZE, 100)).toBe(false);
    expect(canShowMore(100, 100, 100)).toBe(false);
  });

  it("does not appear when the maximum is the first page", () => {
    expect(canShowMore(40, 40, 40)).toBe(false);
  });
});

describe("the words of the screen", () => {
  it("counts the results and the time of the request", () => {
    expect(summaryText(14, 130)).toBe("14 resultados · 0,13 s");
    expect(summaryText(1, 2_345)).toBe("1 resultado · 2,35 s");
  });

  it("says how many photos a filter left out, over the whole collection", () => {
    expect(excludedText(112, "date")).toBe(
      "112 fotos ficaram de fora porque não têm data de captura.",
    );
    expect(excludedText(1_204, "position")).toBe(
      "1.204 fotos ficaram de fora porque não têm posição.",
    );
    expect(excludedText(1, "position")).toBe(
      "1 foto ficou de fora porque não tem posição.",
    );
  });
});
