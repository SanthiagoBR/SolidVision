/**
 * The number in the corner of a card (RFC-038 sections 2.10 and 5.3).
 *
 * The cosine between the search and the photo, as the API sent it: no
 * rescale, no cut (RFC-025 section 14), three places and a comma -- the CLI's
 * format (`format_hits()`) -- because a CLIP's cosines sit in a narrow band
 * and two places would merge photos the order separates. A negative keeps its
 * sign. **Never a percentage**, which would give an uncalibrated cosine the
 * look of a probability (RFC-031 section 12).
 *
 * The number is a product decision of RFC-038 that undoes the roadmap's "no
 * score on the cards": harder to read than the Figma's three dots, and truer.
 */

const MINUS = "−";

/** `0.3124` as `0,312`; `-0.05` as `−0,050`. */
export function formatSimilarity(similarity: number): string {
  const fixed = Math.abs(similarity).toFixed(3).replace(".", ",");
  return similarity < 0 ? `${MINUS}${fixed}` : fixed;
}

/** What the `title` and a screen reader say of it. */
export function similarityTitle(similarity: number): string {
  return (
    `Similaridade ${formatSimilarity(similarity)}: o cosseno entre a sua busca e a ` +
    `foto, de ${MINUS}1 a 1. Só compara fotos da mesma busca.`
  );
}

export const GRID_FOOTNOTE =
  "Ordenado por similaridade. O número é o cosseno entre a busca e a foto, não " +
  "uma porcentagem, e só compara fotos da mesma busca.";
