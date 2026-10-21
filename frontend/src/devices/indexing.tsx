/**
 * The job runner, as the page knows it (RFC-039 sections 2.3, 5.8 and 5.9).
 *
 * The shell keeps its last state, and the page asks for it when it mounts:
 * a runner that refused to start -- models missing, exit 78 -- may have said
 * so before the page existed, and a reload of "Reconectando…" would otherwise
 * lose it. The events after that arrive through `onLauncherEvent`.
 *
 *     state         the page
 *     unavailable   a banner on every screen, with the line of the event and
 *                   "Abrir a pasta de logs"; the buttons that index are
 *                   disabled with that reason. The launcher restarts it no
 *                   more (RFC-036 section 6.1): it takes opening the app again
 *     restarting    no banner. A job that was running when it came gets the
 *                   note of `RESTART_NOTE`, until the job changes state
 *     null          a browser, or a window that does not own the launcher:
 *                   nothing shows, and nothing claims that indexing works
 */

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, type ReactNode, useContext, useEffect, useState } from "react";

import type { Job } from "../api/client";
import { keys } from "../api/keys";
import { bridge, type IndexingStatus } from "../shell/bridge";
import styles from "./Devices.module.css";
import { runKey } from "./panel";

export interface IndexingValue {
  readonly status: IndexingStatus | null;
  /** The runs -- `runKey()` -- that were going when the executor restarted. */
  readonly restarted: ReadonlySet<string>;
}

const NOTHING: IndexingValue = { status: null, restarted: new Set() };

const IndexingContext = createContext<IndexingValue>(NOTHING);

export function useIndexing(): IndexingValue {
  return useContext(IndexingContext);
}

/** The runs going right now, from the jobs in the cache: the cards poll every
 * active job, so a running one is there. */
function runningNow(client: QueryClient): string[] {
  return client
    .getQueryCache()
    .findAll({ queryKey: keys.allJobs })
    .map((query) => query.state.data as Job | undefined)
    .filter((job): job is Job => job !== undefined && job.status === "running")
    .map(runKey);
}

export function IndexingProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [value, setValue] = useState<IndexingValue>(NOTHING);

  useEffect(() => {
    const shell = bridge();
    if (shell === null) {
      return;
    }
    let mounted = true;
    shell
      .indexing?.()
      .then((status) => {
        if (mounted && status !== null) {
          setValue((current) => ({ ...current, status }));
        }
      })
      .catch(() => undefined);
    const unsubscribe = shell.onLauncherEvent((event) => {
      if (
        event.event !== "child" ||
        event.name !== "job_runner" ||
        typeof event.state !== "string"
      ) {
        return;
      }
      const status: IndexingStatus = {
        state: event.state,
        detail: event.detail ?? null,
      };
      const restarted = event.state === "restarting" ? runningNow(client) : [];
      setValue((current) => ({
        status,
        restarted:
          restarted.length > 0
            ? new Set([...current.restarted, ...restarted])
            : current.restarted,
      }));
    });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [client]);

  return <IndexingContext.Provider value={value}>{children}</IndexingContext.Provider>;
}

export const UNAVAILABLE_TEXT =
  "A indexação está indisponível. A busca continua funcionando, e nenhum disco é " +
  "indexado até o SolidVision ser aberto de novo.";

/** The banner of section 5.8, at the top of the content, on every screen. */
export function IndexingBanner() {
  const { status } = useIndexing();
  if (status?.state !== "unavailable") {
    return null;
  }
  const shell = bridge();
  return (
    <div className={styles.banner} role="alert">
      <div className={styles.bannerBody}>
        <p className={styles.bannerText}>{UNAVAILABLE_TEXT}</p>
        {status.detail !== null && status.detail !== "" && (
          <p className={styles.diagnostic}>{status.detail}</p>
        )}
      </div>
      {shell !== null && (
        <button
          type="button"
          className={styles.bannerAction}
          onClick={() => shell.openLogsFolder()}
        >
          Abrir a pasta de logs
        </button>
      )}
    </div>
  );
}
