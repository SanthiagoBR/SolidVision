/**
 * `/busca`: the search, its grid and the detail over it (RFC-038 section 5).
 *
 *     when                                          the grid shows
 *     no search                                     the home screen
 *     running, with /health `loading`; or for more  "Carregando o modelo de
 *     than 1 s with it `on_demand`                  busca…" (15.0 s, RFC-036 16)
 *     running, otherwise                            the results before, faded
 *     no result, and no photo indexed at all        "Nenhuma foto indexada ainda."
 *     no result                                     "Nenhuma foto encontrada."
 *     excluded_unknown_date > 0                     the amber notice, and its
 *     excluded_unknown_position > 0                 button, above the grid
 *     an error                                      the sentence of section 5.7
 *
 * **The first page is 40**, explicit, and "Mostrar mais" -- when 40 of 40 came
 * -- asks for the `max_search_limit` at once. The grid keeps the 40 while the
 * 100 arrive, then shows the 100 in their own order: with the `ef_search` of
 * section 4.3 their first 40 may not be the first page's. Fewer than asked for
 * means there is no more, and the button does not appear.
 */

import {
  keepPreviousData,
  type QueryClient,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import { useDevices, useHealth } from "../api/queries";
import { PAGE_SIZE, useSearchActions } from "./actions";
import {
  searchByImage,
  searchByText,
  searchSimilar,
  type SearchPage,
  useCapabilities,
} from "./api";
import { type DroppedPhoto, useDroppedPhoto } from "./dropped-photo";
import { errorMessage, type Route } from "./messages";
import {
  apiQuery,
  type Filters,
  hasFilters,
  readSearchUrl,
  type SearchQuery,
  searchKeyOf,
  searchQueryString,
  type SearchUrl,
} from "./params";
import { PhotoDetail } from "./PhotoDetail";
import { ResultGrid } from "./ResultGrid";
import { SearchControls } from "./SearchControls";
import styles from "./SearchScreen.module.css";
import { GRID_FOOTNOTE } from "./similarity";
import { finishGesture, startGesture } from "./timing";

export const MODEL_LOADING_TEXT =
  "Carregando o modelo de busca. A primeira busca depois de abrir o SolidVision " +
  "espera por ele.";
const ON_DEMAND_GRACE_MS = 1_000;

/** Whether "Mostrar mais" is drawn: 40 of 40 came, and more may be asked. */
export function canShowMore(results: number, limit: number, maxLimit: number): boolean {
  return limit < maxLimit && results === limit;
}

const COUNT = new Intl.NumberFormat("pt-BR");
const SECONDS = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** `14 resultados · 0,13 s`. */
export function summaryText(results: number, requestMs: number): string {
  const noun = results === 1 ? "resultado" : "resultados";
  return `${COUNT.format(results)} ${noun} · ${SECONDS.format(requestMs / 1000)} s`;
}

export function excludedText(count: number, reason: "date" | "position"): string {
  const photos = count === 1 ? "1 foto ficou" : `${COUNT.format(count)} fotos ficaram`;
  const have = count === 1 ? "tem" : "têm";
  return reason === "date"
    ? `${photos} de fora porque não ${have} data de captura.`
    : `${photos} de fora porque não ${have} posição.`;
}

function routeOf(query: SearchQuery): Route {
  return query.kind === "text"
    ? "search"
    : query.kind === "similar"
      ? "similar"
      : "upload";
}

export function SearchScreen() {
  const [params] = useSearchParams();
  const url = useMemo(() => readSearchUrl(params), [params]);
  if (url.query === null) {
    const rest = params.toString();
    return <Navigate to={rest === "" ? "/" : `/?${rest}`} replace />;
  }
  return <Search url={url} query={url.query} />;
}

function Search({ url, query }: { url: SearchUrl; query: SearchQuery }) {
  const actions = useSearchActions(url);
  const queryClient = useQueryClient();
  const health = useHealth();
  const devices = useDevices();
  const capabilities = useCapabilities();
  const photo = useDroppedPhoto();
  const maxLimit = capabilities.data?.max_search_limit ?? PAGE_SIZE;
  const minRadius = capabilities.data?.min_radius_m ?? null;

  // The page size belongs to one search: a new one starts again at 40.
  const identity =
    searchQueryString({ ...url, photo: null }) +
    (query.kind === "image" ? `#${photo?.token ?? 0}` : "");
  const [paging, setPaging] = useState({ identity, limit: PAGE_SIZE });
  const limit = paging.identity === identity ? paging.limit : PAGE_SIZE;

  const needsRadius = url.filters.near !== null && minRadius === null;
  const missingPhoto = query.kind === "image" && photo === null;
  const api = needsRadius ? null : apiQuery(url.filters, limit, minRadius ?? 0);
  const key = ["search", searchKeyOf(query, photo?.token ?? null), api] as const;

  const search = useQuery({
    queryKey: key,
    enabled: api !== null && !missingPhoto,
    queryFn: ({ signal }): Promise<SearchPage> =>
      api === null
        ? Promise.reject(new Error("The radius floor is not known yet"))
        : run(query, photo, api, signal),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: (failures, error) =>
      error instanceof ApiError && error.status === 0 && failures < 1,
  });

  const page = search.data;
  const fresh = page !== undefined && !search.isPlaceholderData && !search.isFetching;

  const modelLoading = useModelLoading(query, search.isFetching, health.data?.search);
  const paint = usePaintMark(query, limit, fresh ? page : undefined);

  const onText = (q: string) => {
    const href = actions.text(q);
    if (href === `/busca?${searchQueryString({ ...url, photo: null })}`) {
      // The same search again: run it again, from the first page.
      setPaging({ identity, limit: PAGE_SIZE });
      void queryClient.invalidateQueries({
        queryKey: ["search", searchKeyOf(query, photo?.token ?? null)],
      });
    }
  };

  const more = () => {
    startGesture(query.kind, maxLimit);
    setPaging({ identity, limit: maxLimit });
  };

  const error = search.error;
  // -1: an error that is not an answer of the API, shown as a failure.
  const status = error instanceof ApiError ? error.status : error ? -1 : null;
  const route = routeOf(query);
  useEffect(() => {
    if (status === 400) {
      // A defect: the interface sends no radius below the minimum and no
      // inverted range. The status goes to shell.log, without the detail.
      console.error(`A busca respondeu 400 (${route})`);
    }
  }, [status, route]);

  const indexedTotal = (devices.data ?? []).reduce(
    (sum, device) => sum + device.indexed_images,
    0,
  );
  // With no answer at all, the screen keeps the last grid (section 5.7).
  const shown = page ?? (status === 0 ? lastPage(queryClient) : undefined);
  const opened =
    url.photo !== null ? shown?.results.find((r) => r.id === url.photo) : undefined;

  return (
    <div className={styles.screen}>
      <SearchControls
        text={query.kind === "text" ? query.q : ""}
        filters={url.filters}
        photo={query.kind === "image" ? photo : null}
        onText={onText}
        onPhoto={(file) => {
          actions.photo(file);
        }}
        onClearPhoto={actions.home}
        onFilters={actions.filters}
        summary={fresh ? summaryText(page.results.length, page.requestMs) : undefined}
      />

      {missingPhoto && (
        <p className={styles.state}>
          Solte a foto de novo para buscar parecidas: ela não fica guardada quando a
          janela recarrega.
        </p>
      )}
      {modelLoading && <p className={styles.state}>{MODEL_LOADING_TEXT}</p>}
      {status !== null && (
        <p className={styles.error} role="alert">
          {errorMessage(
            route,
            status === -1 ? 500 : status,
            error instanceof ApiError ? error.detail : null,
          )}
        </p>
      )}

      {shown !== undefined && (
        <Notices page={shown} filters={url.filters} onFilters={actions.filters} />
      )}

      {shown !== undefined && shown.results.length === 0 && !search.isFetching && (
        <Empty
          indexedTotal={indexedTotal}
          known={devices.data !== undefined}
          filtered={hasFilters(url.filters)}
        />
      )}

      {shown !== undefined && shown.results.length > 0 && (
        <>
          <ResultGrid
            results={shown.results}
            faded={search.isFetching || status !== null}
            onOpen={actions.openPhoto}
            onSettled={paint}
          />
          {fresh && canShowMore(page.results.length, limit, maxLimit) && (
            <button type="button" className={styles.more} onClick={more}>
              Mostrar mais
            </button>
          )}
          <p className={styles.footnote}>{GRID_FOOTNOTE}</p>
        </>
      )}

      {url.photo !== null && (
        <PhotoDetail
          key={url.photo}
          imageId={url.photo}
          initial={opened}
          onClose={actions.closePhoto}
          onSimilar={actions.similar}
          onNear={actions.near}
        />
      )}
    </div>
  );
}

/** The search answered most recently, of those still in the cache. */
function lastPage(queryClient: QueryClient): SearchPage | undefined {
  let latest: { at: number; page: SearchPage } | undefined;
  for (const cached of queryClient.getQueryCache().findAll({ queryKey: ["search"] })) {
    const page = cached.state.data as SearchPage | undefined;
    if (
      page !== undefined &&
      (latest === undefined || cached.state.dataUpdatedAt > latest.at)
    ) {
      latest = { at: cached.state.dataUpdatedAt, page };
    }
  }
  return latest?.page;
}

function run(
  query: SearchQuery,
  photo: DroppedPhoto | null,
  api: ReturnType<typeof apiQuery>,
  signal: AbortSignal,
): Promise<SearchPage> {
  switch (query.kind) {
    case "text":
      return searchByText(query.q, api, signal);
    case "similar":
      return searchSimilar(query.imageId, api, signal);
    case "image":
      if (photo === null) {
        return Promise.reject(new Error("No dropped photo"));
      }
      return searchByImage(photo.file, api, signal);
  }
}

/** The model's text: while a search that needs it runs and `/health` says
 * `loading`, or after 1 s of one with `on_demand`. "Parecidas com esta"
 * needs no model, and never shows it. */
function useModelLoading(
  query: SearchQuery,
  fetching: boolean,
  state: string | undefined,
): boolean {
  const [late, setLate] = useState(false);
  const needsModel = query.kind !== "similar";
  useEffect(() => {
    if (!fetching || !needsModel || state !== "on_demand") {
      return;
    }
    const timer = setTimeout(() => setLate(true), ON_DEMAND_GRACE_MS);
    return () => {
      clearTimeout(timer);
      setLate(false);
    };
  }, [fetching, needsModel, state]);
  if (!fetching || !needsModel) {
    return false;
  }
  return state === "loading" || (state === "on_demand" && late);
}

/** The mark of section 5.6: the frame after every thumbnail of a fresh page
 * settled. A card reused from the page before is already settled. */
function usePaintMark(
  query: SearchQuery,
  limit: number,
  page: SearchPage | undefined,
): (imageId: string) => void {
  const settled = useRef(new Set<string>());
  const current = useRef<{ page: SearchPage | undefined; limit: number }>({
    page,
    limit,
  });
  useEffect(() => {
    current.current = { page, limit };
  });

  const check = useCallback(() => {
    const { page: shown, limit: asked } = current.current;
    if (shown === undefined) {
      return;
    }
    if (!shown.results.every((result) => settled.current.has(result.id))) {
      return;
    }
    requestAnimationFrame(() =>
      requestAnimationFrame(() =>
        finishGesture(
          {
            kind: query.kind,
            limit: asked,
            results: shown.results.length,
            receivedAt: shown.receivedAt,
          },
          performance.now(),
        ),
      ),
    );
  }, [query.kind]);

  useEffect(() => {
    if (page !== undefined) {
      check();
    }
  }, [page, check]);

  return useCallback(
    (imageId: string) => {
      settled.current.add(imageId);
      check();
    },
    [check],
  );
}

function Notices({
  page,
  filters,
  onFilters,
}: {
  page: SearchPage;
  filters: Filters;
  onFilters: (filters: Filters) => void;
}) {
  const date = page.excludedUnknownDate ?? 0;
  const position = page.excludedUnknownPosition ?? 0;
  if (date === 0 && position === 0) {
    return null;
  }
  return (
    <div className={styles.notices}>
      {date > 0 && (
        <div className={styles.notice}>
          <p className={styles.noticeText}>{excludedText(date, "date")}</p>
          <button
            type="button"
            className={styles.noticeAction}
            onClick={() => onFilters({ ...filters, from: null, to: null })}
          >
            Tirar o filtro de datas
          </button>
        </div>
      )}
      {position > 0 && (
        <div className={styles.notice}>
          <p className={styles.noticeText}>{excludedText(position, "position")}</p>
          <button
            type="button"
            className={styles.noticeAction}
            onClick={() =>
              onFilters({ ...filters, near: null, radius: null, nearOf: null })
            }
          >
            Tirar o filtro de posição
          </button>
        </div>
      )}
    </div>
  );
}

function Empty({
  indexedTotal,
  known,
  filtered,
}: {
  indexedTotal: number;
  known: boolean;
  filtered: boolean;
}) {
  if (known && indexedTotal === 0) {
    return <p className={styles.state}>Nenhuma foto indexada ainda.</p>;
  }
  return (
    <p className={styles.state}>
      {filtered
        ? "Nenhuma foto encontrada com esses filtros."
        : "Nenhuma foto encontrada."}
    </p>
  );
}
