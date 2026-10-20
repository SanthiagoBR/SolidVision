/**
 * The home screen, node 44:458 of the Figma, with real numbers (RFC-037 7).
 *
 * The search bar, the pills and the example queries are drawn here and wired
 * by RFC-038: until then they are disabled, rather than taking a query they
 * would not run. The notice's "Escolher pastas" arrives with the folders
 * screen, in RFC-039.
 */

import { useEffect, useRef } from "react";

import { useCompletedJobs, useDevices, useHealth } from "../api/queries";
import { bridge } from "../shell/bridge";
import styles from "./Home.module.css";
import { searchStatusText } from "./search-status";
import {
  devicePillText,
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
  const devices = useDevices();
  const jobs = useCompletedJobs();
  const health = useHealth();
  const summary =
    devices.data !== undefined && jobs.data !== undefined
      ? summarize(devices.data, jobs.data)
      : null;
  useReportHome(summary);
  const status = searchStatusText(health.data);
  const notice = summary === null ? null : neverIndexedText(summary.neverIndexed);

  return (
    <div className={styles.home}>
      <label className={styles.searchbar}>
        <span className={styles.searchIcon} aria-hidden="true">
          ⌕
        </span>
        <input
          className={styles.searchInput}
          type="search"
          placeholder="Descreva a foto que você procura…"
          aria-label="Descreva a foto que você procura"
          disabled
        />
        {status !== null && (
          <span className={styles.searchStatus} role="status">
            {status}
          </span>
        )}
      </label>

      <div className={styles.filters}>
        <button type="button" className={styles.pillSelected} disabled>
          {devicePillText(summary?.devices ?? null)}
          <span aria-hidden="true">▾</span>
        </button>
        <button type="button" className={styles.pill} disabled>
          Filtros
          <span aria-hidden="true">▾</span>
        </button>
      </div>

      <section className={styles.hero}>
        <div className={styles.emptyState}>
          <h1 className={styles.title}>
            Busque por descrição, não por nome de arquivo
          </h1>
          <p className={styles.lede}>
            Escreva o que aparece na foto. A busca cobre os dispositivos indexados,
            mesmo os que estão desconectados agora.
          </p>
          <div className={styles.examples}>
            {EXAMPLE_QUERIES.map((query) => (
              <button key={query} type="button" className={styles.example} disabled>
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
