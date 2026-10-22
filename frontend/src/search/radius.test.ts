import { describe, expect, it } from "vitest";

import {
  defaultRadius,
  effectiveRadius,
  formatRadius,
  nearFilterText,
  nearTitleText,
  radiusOptions,
} from "./radius";

describe("the words of an active circle (RFC-040 section 2.4)", () => {
  it("say the map's point, for a circle no photo gave", () => {
    expect(nearFilterText(10_000, null)).toBe("Até 10 km do ponto do mapa");
    expect(nearFilterText(1_000, null)).toBe("Até 1 km do ponto do mapa");
    expect(nearTitleText(null)).toBe("Perto do ponto do mapa");
  });

  it("say the photo's name, as before", () => {
    expect(nearFilterText(1_000, "DJI_0042.JPG")).toBe("Até 1 km de DJI_0042.JPG");
    expect(nearFilterText(300, "uma foto")).toBe("Até 300 m de uma foto");
    expect(nearTitleText("DJI_0042.JPG")).toBe("Perto de DJI_0042.JPG");
  });
});

describe("the radius list", () => {
  it("starts at the min_radius_m of /capabilities", () => {
    expect(radiusOptions(300)).toEqual([300, 1_000, 5_000, 10_000, 25_000, 100_000]);
    expect(radiusOptions(450)).toEqual([450, 1_000, 5_000, 10_000, 25_000, 100_000]);
    expect(radiusOptions(2_000)).toEqual([2_000, 5_000, 10_000, 25_000, 100_000]);
  });

  it("has 10 km, for the cells of a state's zoom (RFC-040 section 2.4)", () => {
    expect(radiusOptions(300)).toContain(10_000);
    expect(formatRadius(10_000)).toBe("10 km");
  });

  it("is 1 km when nobody chose, unless the floor is above it", () => {
    expect(defaultRadius(300)).toBe(1_000);
    expect(effectiveRadius(null, 300)).toBe(1_000);
    expect(defaultRadius(2_000)).toBe(2_000);
  });

  it("keeps a choice the floor allows, and drops one it does not", () => {
    expect(effectiveRadius(5_000, 300)).toBe(5_000);
    expect(effectiveRadius(200, 300)).toBe(1_000);
  });

  it("says metres below a kilometre, and kilometres above", () => {
    expect(formatRadius(300)).toBe("300 m");
    expect(formatRadius(1_000)).toBe("1 km");
    expect(formatRadius(2_500)).toBe("2,5 km");
    expect(formatRadius(100_000)).toBe("100 km");
  });
});
