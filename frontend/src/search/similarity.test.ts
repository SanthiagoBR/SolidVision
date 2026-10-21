import { describe, expect, it } from "vitest";

import { formatSimilarity, similarityTitle } from "./similarity";

describe("the number of the card", () => {
  it("has three places and a comma, never a percentage", () => {
    expect(formatSimilarity(0.3124)).toBe("0,312");
    expect(formatSimilarity(1)).toBe("1,000");
    expect(formatSimilarity(0.2996)).toBe("0,300");
  });

  it("keeps the sign of a negative cosine, with a minus sign", () => {
    expect(formatSimilarity(-0.05)).toBe("−0,050");
  });

  it("is explained to whoever hovers or reads it aloud", () => {
    expect(similarityTitle(0.3124)).toBe(
      "Similaridade 0,312: o cosseno entre a sua busca e a foto, de −1 a 1. " +
        "Só compara fotos da mesma busca.",
    );
  });
});
