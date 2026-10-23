/**
 * `/busca?todas`: every photo of a circle, by capture date (RFC-044 section 6).
 *
 * The search screen with no query: the bar, the pills and the active filters as
 * always, the header where the search counts its results, the two directions
 * beside it, the grid with the date in the corner of each card, the divider
 * before the undated, and "Mostrar mais" -- which here **appends** the next
 * page, by a cursor, until the list is over.
 *
 *     when                                     the screen shows
 *     the first page running                   nothing, or the list before,
 *                                              faded; never the model's text
 *     a next page running                      the pages before, and the button
 *                                              disabled with "Lendo…"
 *     no photo, and none indexed               "Nenhuma foto indexada ainda."
 *     no photo in the circle                   "Nenhuma foto até 1 km daqui.",
 *                                              "…com esses filtros"
 *     a 404 on a next page                     "A lista mudou desde a última
 *                                              página.", with "Recomeçar"
 *
 * **One entry of the cache holds the pages**, a `useInfiniteQuery` whose page
 * parameter is the `next_after`. The first page is the search's 40, every one
 * after it the `max_search_limit`, 100. The pages and the cursor are not in the
 * URL: a reload -- the "Reconectando…" -- starts again from the first page.
 */

import {
  keepPreviousData,
  useInfiniteQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { ApiError } from "../api/client";
import { useDevices } from "../api/queries";
import { browseKey, PAGE_SIZE, useSearchActions } from "./actions";
import { browseImages, type BrowsePage, useCapabilities } from "./api";
import {
  BROWSE_FOOTNOTE,
  BROWSE_PROMPT,
  browseHeaderText,
  browsePageLimit,
  canBrowseMore,
  dateCorner,
  DIVIDER_NOTE,
  dividerTitle,
  emptyBrowseText,
  firstUndatedId,
  flattenPages,
  ORDER_LABELS,
  progressText,
  READING_MORE,
  RESTART,
} from "./browse";
import { errorMessage } from "./messages";
import { Notices } from "./Notices";
import {
  apiBrowseQuery,
  apiFilters,
  type BrowseOrder,
  type SearchQuery,
  type SearchUrl,
} from "./params";
import { PhotoDetail } from "./PhotoDetail";
import { effectiveRadius } from "./radius";
import { type Divider, ResultGrid } from "./ResultGrid";
import { SearchControls } from "./SearchControls";
import styles from "./SearchScreen.module.css";
import { finishGesture, startGesture } from "./timing";

export type BrowseQuery = Extract<SearchQuery, { kind: "browse" }>;

const ORDERS: readonly BrowseOrder[] = ["newest", "oldest"];

export function BrowseScreen({ url, query }: { url: SearchUrl; query: BrowseQuery }) {
  const actions = useSearchActions(url);
  const queryClient = useQueryClient();
  const devices = useDevices();
  const capabilities = useCapabilities();
  const maxLimit = capabilities.data?.max_search_limit ?? PAGE_SIZE;
  const minRadius = capabilities.data?.min_radius_m ?? null;
  const filters = minRadius === null ? null : apiFilters(url.filters, minRadius);
  const key = browseKey(query.order, filters);

  const list = useInfiniteQuery({
    queryKey: key,
    enabled: filters !== null,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }): Promise<BrowsePage> =>
      browseImages(
        apiBrowseQuery(
          url.filters,
          query.order,
          browsePageLimit(pageParam, maxLimit),
          pageParam,
          minRadius ?? 0,
        ),
        signal,
      ),
    getNextPageParam: (last) => last.nextAfter,
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: (failures, error) =>
      error instanceof ApiError && error.status === 0 && failures < 1,
  });

  const pages = list.data?.pages ?? [];
  const results = flattenPages(pages);
  const summary = pages[0]?.summary ?? null;
  // The pages on the screen are this list's, and not the list before it.
  const current = list.data !== undefined && !list.isPlaceholderData;
  const firstRunning = list.isFetching && !list.isFetchingNextPage;
  const paint = useBrowsePaintMark(current ? pages.at(-1) : undefined);

  const error = list.error;
  // -1: an error that is not an answer of the API, shown as a failure.
  const status = error instanceof ApiError ? error.status : error ? -1 : null;
  const changed = status === 404 && list.isFetchNextPageError;
  useEffect(() => {
    if (status === 400) {
      // A defect: the interface sends no radius below the minimum and no
      // inverted range. The status goes to shell.log, without the detail.
      console.error("A busca respondeu 400 (browse)");
    }
  }, [status]);

  const restart = () => {
    startGesture("browse", PAGE_SIZE);
    void queryClient.resetQueries({ queryKey: key, exact: true });
  };
  const more = () => {
    startGesture("browse", maxLimit);
    void list.fetchNextPage();
  };

  const indexedTotal = (devices.data ?? []).reduce(
    (sum, device) => sum + device.indexed_images,
    0,
  );
  const otherFilters =
    url.filters.devices.length > 0 ||
    url.filters.from !== null ||
    url.filters.to !== null;
  const undatedId = firstUndatedId(results);
  const divider: Divider | null =
    undatedId === null
      ? null
      : {
          beforeId: undatedId,
          title: dividerTitle(
            summary?.undated ?? results.filter((r) => r.captured_at === null).length,
          ),
          note: DIVIDER_NOTE,
        };

  const directions = (
    <span className={styles.order} role="group" aria-label="Ordem">
      {ORDERS.map((order) => (
        <button
          key={order}
          type="button"
          className={order === query.order ? styles.orderPressed : styles.orderButton}
          aria-pressed={order === query.order}
          onClick={() => {
            if (order !== query.order) {
              actions.browse(order);
            }
          }}
        >
          {ORDER_LABELS[order]}
        </button>
      ))}
    </span>
  );

  return (
    <div className={styles.screen}>
      <SearchControls
        text=""
        query={query}
        filters={url.filters}
        photo={null}
        onText={actions.text}
        onPhoto={(file) => {
          actions.photo(file);
        }}
        onClearPhoto={actions.home}
        onFilters={actions.filters}
        summary={current && summary !== null ? browseHeaderText(summary) : undefined}
        extra={directions}
        prompt={BROWSE_PROMPT}
      />

      {status !== null && (
        <div className={styles.error} role="alert">
          {errorMessage("browse", status === -1 ? 500 : status)}
          {changed && (
            <button type="button" className={styles.restart} onClick={restart}>
              {RESTART}
            </button>
          )}
        </div>
      )}

      {summary !== null && (
        <Notices
          excludedUnknownDate={summary.excludedUnknownDate}
          excludedUnknownPosition={summary.excludedUnknownPosition}
          onClearDates={() => actions.filters({ ...url.filters, from: null, to: null })}
        />
      )}

      {current && !list.isFetching && results.length === 0 && status === null && (
        <p className={styles.state}>
          {devices.data !== undefined && indexedTotal === 0
            ? "Nenhuma foto indexada ainda."
            : emptyBrowseText(
                effectiveRadius(url.filters.radius, minRadius ?? 0),
                otherFilters,
              )}
        </p>
      )}

      {results.length > 0 && (
        <>
          <ResultGrid
            results={results}
            corner={dateCorner}
            divider={divider}
            faded={!current || firstRunning || (status !== null && !changed)}
            onOpen={actions.openPhoto}
            onSettled={paint}
          />
          {current && !changed && (canBrowseMore(pages) || list.isFetchingNextPage) && (
            <div className={styles.moreRow}>
              <button
                type="button"
                className={styles.more}
                onClick={more}
                disabled={list.isFetchingNextPage}
              >
                {list.isFetchingNextPage ? READING_MORE : "Mostrar mais"}
              </button>
              {summary !== null && (
                <span className={styles.moreCount}>
                  {progressText(results.length, summary.total)}
                </span>
              )}
            </div>
          )}
          <p className={styles.footnote}>{BROWSE_FOOTNOTE}</p>
        </>
      )}

      {url.photo !== null && (
        <PhotoDetail
          key={url.photo}
          imageId={url.photo}
          initial={results.find((result) => result.id === url.photo)}
          onClose={actions.closePhoto}
          onSimilar={actions.similar}
          onNear={actions.near}
        />
      )}
    </div>
  );
}

/** The mark of RFC-038 section 5.6, for a listing: from the gesture -- the
 * action, "Mostrar mais" or a direction -- to the frame after the last
 * thumbnail of the page it asked for. The pages before are already settled. */
function useBrowsePaintMark(last: BrowsePage | undefined): (imageId: string) => void {
  const settled = useRef(new Set<string>());
  const current = useRef(last);
  useEffect(() => {
    current.current = last;
  });

  const check = useCallback(() => {
    const page = current.current;
    if (page === undefined) {
      return;
    }
    if (!page.results.every((result) => settled.current.has(result.id))) {
      return;
    }
    requestAnimationFrame(() =>
      requestAnimationFrame(() =>
        finishGesture(
          {
            kind: "browse",
            limit: page.limit,
            results: page.results.length,
            receivedAt: page.receivedAt,
          },
          performance.now(),
        ),
      ),
    );
  }, []);

  useEffect(() => {
    if (last !== undefined) {
      check();
    }
  }, [last, check]);

  return useCallback(
    (imageId: string) => {
      settled.current.add(imageId);
      check();
    },
    [check],
  );
}
