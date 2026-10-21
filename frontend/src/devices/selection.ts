/**
 * The folders chosen to index, on one device (RFC-039 sections 2.6 and 5.6).
 *
 * A set of canonical paths -- the `path` of the listing, which is what goes
 * back in `scopes[]` -- that **only includes**: a scope is a folder and
 * everything under it, and `scopes` has no exclusion. So marking a folder
 * takes out of the set the folders under it, as `normalize_scopes()` would,
 * and the folders under a marked one show as marked and locked, "incluída em
 * 2018". Taking one out from inside a marked folder would be an exclusion the
 * API does not have.
 *
 * Paths are compared by their parts, never as text: `2018` is not a part of
 * `2018b` (`JobScope.contains()`). Exactly, as the API compares them: the
 * listing gives the filesystem's spelling, and it is the one kept.
 *
 * Pure, and immutable: every change is a new set.
 */

export type Selection = ReadonlySet<string>;

export const EMPTY: Selection = new Set<string>();

function partsOf(path: string): string[] {
  return path.split("/").filter((part) => part !== "");
}

/** Whether `path` is `ancestor` or lies under it. */
export function isWithin(path: string, ancestor: string): boolean {
  const inner = partsOf(path);
  const outer = partsOf(ancestor);
  if (outer.length > inner.length) {
    return false;
  }
  return outer.every((part, index) => inner[index] === part);
}

/** The marked folder that includes `path` from above, if one does. */
export function includedIn(selection: Selection, path: string): string | null {
  for (const chosen of selection) {
    if (chosen !== path && isWithin(path, chosen)) {
      return chosen;
    }
  }
  return null;
}

/** Mark `path`: the folders under it leave the set, and a folder already
 * included from above changes nothing. */
export function select(selection: Selection, path: string): Selection {
  if (partsOf(path).length === 0 || selection.has(path)) {
    return selection;
  }
  if (includedIn(selection, path) !== null) {
    return selection;
  }
  const next = new Set<string>();
  for (const chosen of selection) {
    if (!isWithin(chosen, path)) {
      next.add(chosen);
    }
  }
  next.add(path);
  return next;
}

export function deselect(selection: Selection, path: string): Selection {
  if (!selection.has(path)) {
    return selection;
  }
  const next = new Set(selection);
  next.delete(path);
  return next;
}

/** What the `POST /jobs` sends: the marked folders, in a stable order. The API
 * normalizes again and echoes what it will walk. */
export function scopesOf(selection: Selection): string[] {
  return [...selection].sort();
}
