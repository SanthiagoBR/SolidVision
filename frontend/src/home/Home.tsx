/**
 * The home screen, node 44:458 of the Figma, with real numbers (RFC-037 7).
 *
 * Since RFC-038 the search bar, the pills and the example queries are wired:
 * Enter, an example or a dropped photo opens `/busca`, with the filters chosen
 * here. The notice's "Escolher pastas" arrives with the folders screen, in
 * RFC-039.
 */

import { useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router";

import { useCompletedJobs, useDevices } from "../api/queries";
import { useSearchActions } from "../search/actions";
import { readFilters, type SearchUrl } from "../search/params";
import { SearchControls } from "../search/SearchControls";
import { bridge } from "../shell/bridge";
import styles from "./Home.module.css";
import {
  devicesText,
  lastIndexedText,
  neverIndexedText,
  photosText,
  summarize,
  type HomeSummary,
} from "./summary";

export const EXAMPLE_QUERIES = [
  "propriedade rural com lago",
  "telhado azul perto da represa",
  "plantação alagada",
] as const;

export function Home() {
  const [params] = useSearchParams();
  const url: SearchUrl = useMemo(
    () => ({ query: null, filters: readFilters(params), photo: null }),
    [params],
  );
  const actions = useSearchActions(url);
  const devices = useDevices();
  const jobs = useCompletedJobs();
  const summary =
    devices.data !== undefined && jobs.data !== undefined
      ? summarize(devices.data, jobs.data)
      : null;
  useReportHome(summary);
  const notice = summary === null ? null : neverIndexedText(summary.neverIndexed);

  return (
    <div className={styles.home}>
      <SearchControls
        text=""
        filters={url.filters}
        photo={null}
        onText={actions.text}
        onPhoto={(file) => {
          actions.photo(file);
        }}
        onClearPhoto={actions.home}
        onFilters={actions.filters}
      />

      <section className={styles.hero}>
        <div className={styles.emptyState}>
          <h1 className={styles.title}>
            Busque por descrição, não por nome de arquivo
          </h1>
          <p className={styles.lede}>
            Escreva o que aparece na foto, ou solte uma foto na barra. A busca cobre os
            dispositivos indexados, mesmo os que estão desconectados agora.
          </p>
          <div className={styles.examples}>
            {EXAMPLE_QUERIES.map((query) => (
              <button
                key={query}
                type="button"
                className={styles.example}
                onClick={() => actions.text(query)}
              >
                {query}
              </button>
            ))}
          </div>
        </div>

        <Stats summary={summary} failed={devices.isError || jobs.isError} />

        {notice !== null && (
          <div className={styles.notice}>
            <p className={styles.noticeText}>{notice}</p>
          </div>
        )}
      </section>
    </div>
  );
}

function Stats({ summary, failed }: { summary: HomeSummary | null; failed: boolean }) {
  if (summary === null) {
    return (
      <p className={styles.stats} aria-busy={!failed}>
        {failed ? "Não foi possível ler os dispositivos e as indexações." : " "}
      </p>
    );
  }
  if (summary.devices === 0) {
    return <p className={styles.stats}>{devicesText(0)}</p>;
  }
  return (
    <p className={styles.stats}>
      <span>{devicesText(summary.devices)}</span>
      <span>{photosText(summary.indexedImages)}</span>
      <span>{lastIndexedText(summary.lastIndexedAt)}</span>
    </p>
  );
}

/** The mark of section 6.5, once per page: after the frame that painted the
 * numbers. */
function useReportHome(summary: HomeSummary | null): void {
  const reported = useRef(false);
  useEffect(() => {
    const shell = bridge();
    if (summary === null || reported.current || shell === null) {
      return;
    }
    reported.current = true;
    requestAnimationFrame(() =>
      requestAnimationFrame(() =>
        shell.reportHome({
          devices: summary.devices,
          indexed: summary.indexedImages,
          lastIndexedAt: summary.lastIndexedAt?.toISOString() ?? null,
        }),
      ),
    );
  }, [summary]);
}
