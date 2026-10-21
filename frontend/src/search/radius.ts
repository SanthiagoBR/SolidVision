/**
 * The radius of "perto daqui" (RFC-038 section 5.2).
 *
 * Chosen from a list that starts at the `min_radius_m` of `/capabilities` --
 * the floor the API enforces, which the user's file can change -- and 1 km
 * when nobody chose. Not the default RFC-032 section 6 refused: the API does
 * not complete a radius the client did not send, because it cannot know what
 * was meant; here the radius is written on the filter, and what the request
 * carries is what the screen shows. 1 km is a choice, against the aircraft-to-
 * ground offset RFC-032 measured: p95 of 274 m, a maximum of 358 m.
 */

export const STANDARD_RADII_M = [300, 1_000, 5_000, 25_000, 100_000] as const;
export const PREFERRED_RADIUS_M = 1_000;

/** The radii to offer: the floor first, then the standard ones above it. */
export function radiusOptions(minRadiusM: number): number[] {
  const above = STANDARD_RADII_M.filter((radius) => radius > minRadiusM);
  return [minRadiusM, ...above];
}

/** 1 km, unless the floor is above it; then the floor. */
export function defaultRadius(minRadiusM: number): number {
  return Math.max(PREFERRED_RADIUS_M, minRadiusM);
}

/** The radius to send: the chosen one when it is allowed, else the default. */
export function effectiveRadius(chosen: number | null, minRadiusM: number): number {
  if (chosen !== null && Number.isFinite(chosen) && chosen >= minRadiusM) {
    return chosen;
  }
  return defaultRadius(minRadiusM);
}

const NUMBER = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });

/** `300 m`, `1 km`, `2,5 km`. */
export function formatRadius(radiusM: number): string {
  if (radiusM < 1_000) {
    return `${NUMBER.format(radiusM)} m`;
  }
  return `${NUMBER.format(radiusM / 1_000)} km`;
}
