/**
 * Where each gesture goes (RFC-038 section 5.1). Every search is a URL, and
 * every gesture starts the mark of section 5.6 before it navigates.
 *
 * **A gesture runs the search again; going back does not.** The same search
 * is the same key in TanStack Query's cache, kept for good (`staleTime:
 * Infinity`), so back and forward, and the reload of "Reconectando…", show it
 * at once. An Enter, a filter or "Parecidas com esta" marks it stale first:
 * whoever asks again means now.
 *
 * **The listing by date (RFC-044) starts a new list on a gesture.** Its pages
 * are one entry of the cache, and marking it stale would make TanStack Query
 * ask again for every page on the screen, one cursor after the other; so a
 * gesture drops the lists of that direction that no screen shows, and the one
 * it opens starts again from its first page.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router";

import { clearDroppedPhoto, setDroppedPhoto } from "./dropped-photo";
import { uploadRefusal } from "./messages";
import {
  type BrowseOrder,
  type Filters,
  type Point,
  type SearchQuery,
  searchHref,
  searchKeyOf,
  type SearchUrl,
} from "./params";
import { startGesture } from "./timing";

/** The first page: 40, explicit. Up to 40 the database query is the one
 * RFC-025 to 032 measured (RFC-038 section 4.3). */
export const PAGE_SIZE = 40;

/**
 * Where "Buscar perto daqui" of a cell of the map goes (RFC-040 section 5.4):
 * the search the map carries, with its filters and the circle of the cell's
 * centre -- the radius chosen, and no photo, so no `perto_de`. Without a
 * search, the home screen with the circle.
 */
export function nearPointSearch(
  url: SearchUrl,
  point: Point,
  radiusM: number,
): SearchUrl {
  return {
    query: url.query,
    filters: { ...url.filters, near: point, radius: radiusM, nearOf: null },
    photo: null,
  };
}

/** The cache's key of a listing by date: apart from the searches', whose
 * entries are a page each. */
export function browseKey(order: BrowseOrder, api: unknown) {
  return ["browse", searchKeyOf({ kind: "browse", order }, null), api] as const;
}

/**
 * Where "Ver todas as fotos daqui, por data" goes (RFC-044 section 6.2): the
 * listing, with the circle and the filters of the screen, and without the
 * text -- from the home screen with a circle, and from a search with one.
 */
export function browseSearch(url: SearchUrl, order: BrowseOrder = "newest"): SearchUrl {
  return { query: { kind: "browse", order }, filters: url.filters, photo: null };
}

/** The same, from the balloon of a cell of the map: the circle of the cell's
 * centre with the radius that covers it, the map's filters, and not the search
 * the map carries, which is another question. */
export function nearPointBrowse(
  url: SearchUrl,
  point: Point,
  radiusM: number,
): SearchUrl {
  return browseSearch(nearPointSearch(url, point, radiusM));
}

interface DetailState {
  readonly detailFromGrid?: boolean;
}

export function useSearchActions(url: SearchUrl) {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const go = (next: SearchUrl, state?: DetailState) =>
    navigate(searchHref(next), state ? { state } : undefined);
  const fresh = (query: SearchQuery) => {
    if (query.kind === "browse") {
      queryClient.removeQueries({
        queryKey: ["browse", searchKeyOf(query, null)],
        type: "inactive",
      });
    } else if (query.kind !== "image") {
      void queryClient.invalidateQueries({
        queryKey: ["search", searchKeyOf(query, null)],
        refetchType: "none",
      });
    }
  };

  return {
    /** Enter in the bar, or an example query. */
    text(q: string): string {
      startGesture("text", PAGE_SIZE);
      fresh({ kind: "text", q });
      const href = searchHref({
        query: { kind: "text", q },
        filters: url.filters,
        photo: null,
      });
      navigate(href);
      return href;
    },
    /** "Parecidas com esta", with the current filters. */
    similar(imageId: string): void {
      startGesture("similar", PAGE_SIZE);
      fresh({ kind: "similar", imageId });
      go({ query: { kind: "similar", imageId }, filters: url.filters, photo: null });
    },
    /** A dropped or chosen photo; a refusal is returned and nothing is sent. */
    photo(file: File): string | null {
      const refusal = uploadRefusal(file);
      if (refusal !== null) {
        return refusal;
      }
      setDroppedPhoto(file);
      startGesture("image", PAGE_SIZE);
      go({ query: { kind: "image" }, filters: url.filters, photo: null });
      return null;
    },
    /** "Ver todas as fotos daqui, por data", or a direction of the listing:
     * a gesture, which enters the history and starts a new list. */
    browse(order: BrowseOrder = "newest"): void {
      startGesture("browse", PAGE_SIZE);
      fresh({ kind: "browse", order });
      go(browseSearch(url, order));
    },
    /** The same, from the balloon of a cell of the map. */
    browseNear(point: Point, radiusM: number): void {
      startGesture("browse", PAGE_SIZE);
      fresh({ kind: "browse", order: "newest" });
      go(nearPointBrowse(url, point, radiusM));
    },
    /** The × of the dropped photo: back to the home screen, filters kept. */
    home(): void {
      clearDroppedPhoto();
      go({ query: null, filters: url.filters, photo: null });
    },
    /** A filter changed: the current search runs again, whatever its kind. */
    filters(filters: Filters): void {
      const query = url.query;
      if (query !== null) {
        startGesture(query.kind, PAGE_SIZE);
        fresh(query);
      }
      go({ ...url, filters, photo: null });
    },
    /** "Buscar perto daqui": the circle on the current search. */
    near(point: Point, imageId: string): void {
      const query = url.query;
      if (query !== null) {
        startGesture(query.kind, PAGE_SIZE);
        fresh(query);
      }
      go({
        ...url,
        filters: { ...url.filters, near: point, nearOf: imageId },
        photo: null,
      });
    },
    /** "Buscar perto daqui" of a cell of the map: the same gesture, with the
     * cell's centre and the radius that covers it (`nearPointSearch()`). */
    nearPoint(point: Point, radiusM: number): void {
      const query = url.query;
      if (query !== null) {
        startGesture(query.kind, PAGE_SIZE);
        fresh(query);
      }
      go(nearPointSearch(url, point, radiusM));
    },
    openPhoto(imageId: string): void {
      go({ ...url, photo: imageId }, { detailFromGrid: true });
    },
    /** Back, when the detail was opened from the grid; otherwise the same
     * screen without it, so a detail opened by a reload does not leave. */
    closePhoto(): void {
      const state = location.state as DetailState | null;
      if (state?.detailFromGrid) {
        navigate(-1);
      } else {
        navigate(searchHref({ ...url, photo: null }), { replace: true });
      }
    },
  };
}
