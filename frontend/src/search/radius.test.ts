import { describe, expect, it } from "vitest";

import { defaultRadius, effectiveRadius, formatRadius, radiusOptions } from "./radius";

describe("the radius list", () => {
  it("starts at the min_radius_m of /capabilities", () => {
    expect(radiusOptions(300)).toEqual([300, 1_000, 5_000, 25_000, 100_000]);
    expect(radiusOptions(450)).toEqual([450, 1_000, 5_000, 25_000, 100_000]);
    expect(radiusOptions(2_000)).toEqual([2_000, 5_000, 25_000, 100_000]);
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
