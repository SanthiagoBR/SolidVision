import { describe, expect, it } from "vitest";

import {
  BAD_AREA,
  balloonCountLines,
  carriedSearchText,
  centreText,
  hiddenText,
  hintLines,
  legendSizeText,
  MAP_FAILED,
  mapErrorMessage,
  mapState,
  type MapStateInput,
  NONE_HERE_FILTERED_TEXT,
  NONE_HERE_TEXT,
  NONE_INDEXED_TEXT,
  photosText,
  radiusLine,
  searchButtonText,
} from "./words";

describe("the counts (RFC-040 section 5.3)", () => {
  it("are never in the colour alone: the hint and the balloon say them", () => {
    expect(photosText(1)).toBe("1 foto");
    expect(photosText(37)).toBe("37 fotos");
    expect(photosText(1_234)).toBe("1.234 fotos");
    expect(hintLines(37, 2)).toEqual(["37 fotos", "célula de ~1 km"]);
    expect(balloonCountLines(37, 2)).toEqual(["37 fotos", "nesta célula de ~1 km"]);
  });

  it("write a centre with two places and its hemispheres", () => {
    expect(centreText(-26.3214, -48.8163)).toBe("26,32° S, 48,82° O");
    expect(centreText(1.5, 30)).toBe("1,50° N, 30,00° L");
  });
});

describe("the balloon (section 5.4)", () => {
  it("names the search it runs, of each of the three kinds, or none", () => {
    expect(searchButtonText({ kind: "text", q: "telhado azul" }, null)).toBe(
      "Buscar “telhado azul” perto daqui",
    );
    expect(searchButtonText({ kind: "similar", imageId: "x" }, "DJI_0042.JPG")).toBe(
      "Buscar parecidas com DJI_0042.JPG perto daqui",
    );
    expect(searchButtonText({ kind: "image" }, null)).toBe(
      "Buscar parecidas com a foto solta perto daqui",
    );
    expect(searchButtonText(null, null)).toBe("Buscar perto daqui");
  });

  it("says a photo while its name is read", () => {
    expect(searchButtonText({ kind: "similar", imageId: "x" }, null)).toBe(
      "Buscar parecidas com uma foto perto daqui",
    );
  });

  it("says the radius below the button", () => {
    expect(radiusLine(10_000)).toBe("num raio de 10 km do centro da célula");
    expect(radiusLine(300)).toBe("num raio de 300 m do centro da célula");
  });
});

describe("the search the map carries (section 5.5)", () => {
  it("is said above the map, with what the map shows", () => {
    expect(carriedSearchText({ kind: "text", q: "telhado azul" }, null)).toBe(
      "Um clique numa célula busca “telhado azul” perto dali. O mapa mostra todas " +
        "as fotos com posição, e não só as dessa busca.",
    );
    expect(
      carriedSearchText({ kind: "similar", imageId: "x" }, "DJI_0042.JPG"),
    ).toMatch(/^Um clique numa célula busca parecidas com DJI_0042\.JPG perto dali\./);
  });
});

describe("the legend (section 5.3)", () => {
  it("says the size of the cells", () => {
    expect(legendSizeText(1, 1)).toBe("Células de ~11 km");
    expect(legendSizeText(3, 3)).toBe("Células de ~100 m");
  });

  it("says why, when the route made them coarser than the zoom's", () => {
    expect(legendSizeText(1, 0)).toBe(
      "Células de ~110 km, maiores que as deste zoom: a área tem fotos em lugares " +
        "demais para as de ~11 km.",
    );
  });
});

describe("the notices (section 5.5)", () => {
  it("count the photos with no date and with no position, over the collection", () => {
    expect(hiddenText(112, "date")).toBe(
      "112 fotos não aparecem no mapa porque não têm data de captura.",
    );
    expect(hiddenText(1, "date")).toBe(
      "1 foto não aparece no mapa porque não tem data de captura.",
    );
    expect(hiddenText(1_204, "position")).toBe(
      "1.204 fotos não aparecem no mapa porque não têm posição.",
    );
    expect(hiddenText(5, "position")).not.toContain("nesta área");
  });

  it("say nothing with null or zero", () => {
    expect(hiddenText(null, "date")).toBeNull();
    expect(hiddenText(0, "date")).toBeNull();
    expect(hiddenText(0, "position")).toBeNull();
    expect(hiddenText(undefined, "position")).toBeNull();
  });
});

describe("the states of the screen (section 5.5)", () => {
  const quiet: MapStateInput = {
    answered: true,
    late: false,
    fetching: false,
    indexedTotal: 85,
    visibleCells: 12,
    filtered: false,
  };

  it("the first answer, after 300 ms: over the map", () => {
    expect(mapState({ ...quiet, answered: false, fetching: true, late: true })).toEqual(
      {
        kind: "loading",
        where: "centre",
      },
    );
    expect(mapState({ ...quiet, answered: false, fetching: true })).toBeNull();
  });

  it("a later request, after 300 ms: the cells of before, and a corner", () => {
    expect(mapState({ ...quiet, fetching: true, late: true })).toEqual({
      kind: "loading",
      where: "corner",
    });
    expect(mapState({ ...quiet, fetching: true, visibleCells: 0 })).toBeNull();
  });

  it("no photo indexed at all", () => {
    expect(mapState({ ...quiet, indexedTotal: 0, visibleCells: 0 })).toEqual({
      kind: "empty",
      text: NONE_INDEXED_TEXT,
    });
  });

  it("no cell in the visible area, with filters and without", () => {
    expect(mapState({ ...quiet, visibleCells: 0 })).toEqual({
      kind: "empty",
      text: NONE_HERE_TEXT,
    });
    expect(mapState({ ...quiet, visibleCells: 0, filtered: true })).toEqual({
      kind: "empty",
      text: NONE_HERE_FILTERED_TEXT,
    });
    expect(mapState({ ...quiet, visibleCells: 0, indexedTotal: null })?.kind).toBe(
      "empty",
    );
  });

  it("nothing over a map with cells", () => {
    expect(mapState(quiet)).toBeNull();
  });
});

describe("the messages (section 5.7)", () => {
  it("say a 400 and a 422 alike, and leave no answer to the shell", () => {
    expect(mapErrorMessage(400)).toBe(BAD_AREA);
    expect(mapErrorMessage(422)).toBe(BAD_AREA);
    expect(mapErrorMessage(500)).toBe(MAP_FAILED);
    expect(mapErrorMessage(0)).toBeNull();
    expect(mapErrorMessage(null)).toBeNull();
  });
});
