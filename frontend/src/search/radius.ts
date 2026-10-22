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
 *
 * **10 km since RFC-040** (section 2.4): a cell of the map asks for the
 * smallest radius of this list that covers it, and a cell of ~11 km, a state's
 * zoom, would otherwise ask for 25 km -- a circle of 18 times its area.
 */

export const STANDARD_RADII_M = [300, 1_000, 5_000, 10_000, 25_000, 100_000] as const;
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

/**
 * The words of an active circle: `Até 1 km de DJI_0042.JPG`, when a photo gave
 * the centre, and `Até 10 km do ponto do mapa` when a cell of the map did
 * (RFC-040 section 2.4). `photoName` is `null` for a circle with no photo.
 */
export function nearFilterText(radiusM: number, photoName: string | null): string {
  return photoName === null
    ? `Até ${formatRadius(radiusM)} do ponto do mapa`
    : `Até ${formatRadius(radiusM)} de ${photoName}`;
}

/** The title of the "Perto de" section, by the same rule. */
export function nearTitleText(photoName: string | null): string {
  return photoName === null ? "Perto do ponto do mapa" : `Perto de ${photoName}`;
}
