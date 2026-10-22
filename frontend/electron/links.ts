/**
 * The links of the page that open in the default browser (RFC-040 sections
 * 2.8 and 5.6).
 *
 * The window cancels a navigation off its origin at `will-navigate` and denies
 * every `window.open` (RFC-037 section 6). The map's attribution is a link to
 * the OpenStreetMap's copyright page, which the tile usage policy asks to be
 * one click away: **that URL, and only it, opens in the default browser**, and
 * the window stays where it is. The same host with another path, `http:`, or
 * a host that looks like it, stay blocked.
 *
 * No Electron here, as in `events.ts`: `main.ts` asks, and does.
 */

export const BROWSER_LINKS: readonly string[] = [
  "https://www.openstreetmap.org/copyright",
];

/** Whether this URL goes to the default browser: its normalised form is one of
 * the list, exactly -- no query, no fragment, no user, no other port. */
export function opensInBrowser(target: string): boolean {
  let url: URL;
  try {
    url = new URL(target);
  } catch {
    return false;
  }
  return BROWSER_LINKS.includes(url.href);
}
