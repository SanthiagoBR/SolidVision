/**
 * The panel of a device's most recent job (RFC-039 section 5.7).
 *
 *     the job                          the panel says
 *     pending                          "Na fila." Begins when another disk's
 *                                      job ends, or "em instantes"
 *     running, everything at zero      reading the list of files, with an
 *                                      indeterminate bar: the listing gives no
 *                                      sign until it ends (section 2.2)
 *     running                          the counters and the last file saved;
 *                                      for the whole disk, the bar of the
 *                                      device, with the date of the scan
 *     running, cancel_requested        "Cancelando…"
 *     completed                        when, and the counters; the log, if
 *                                      something failed
 *     cancelled                        where it stopped; "Indexar de novo"
 *     failed, the disk away now        it stopped because the disk left;
 *                                      "Indexar de novo" waits for the disk
 *     failed                           the `error_message`, as the line of
 *                                      diagnosis, and the logs
 *
 * **No percentage of the job.** `discovery_complete` turns true only at the
 * end of a job, so a percentage "after the discovery" would appear in the
 * last 512 files of a job of hours; and the counters of a resumed job start
 * again from zero (section 2.2). The only bar is the device's, for the whole
 * disk: `indexed_images` over the denominator its listing wrote.
 *
 * Pure. The `error_message` is free text in English, and is shown only as the
 * line under a title in Portuguese, as the `search_detail` of RFC-037.
 */

import type { Job } from "../api/client";
import { cutLeft } from "./folders";
import { diskBarText, type ScanFacts } from "./share";
import { dayText, foldersCountText, formatCount, instant, timeText } from "./words";

export type PanelAction = "cancel" | "again" | "logs";

export type PanelBar =
  | { readonly kind: "indeterminate" }
  | { readonly kind: "share"; readonly ratio: number; readonly text: string };

export interface Panel {
  readonly tone: "active" | "done" | "attention";
  readonly title: string;
  readonly lines: readonly string[];
  /** The `error_message`, under the title, as a diagnosis. */
  readonly diagnostic: string | null;
  readonly bar: PanelBar | null;
  readonly actions: readonly PanelAction[];
}

export interface PanelContext {
  /** The device's label. */
  readonly label: string;
  /** Whether the disk is plugged in now. */
  readonly connected: boolean;
  /** The device's counts, for the bar of the whole disk. */
  readonly device: ScanFacts;
  /** The label of another device whose job is running, if one is. */
  readonly runningElsewhere: string | null;
  readonly now?: Date;
}

export function isActive(job: Pick<Job, "status">): boolean {
  return job.status === "pending" || job.status === "running";
}

/** `3.204 indexadas agora · 1.200 já estavam no índice · 3 falharam`. */
export function runningCounters(job: Job): string {
  const parts = [
    job.processed_images === 1
      ? "1 indexada agora"
      : `${formatCount(job.processed_images)} indexadas agora`,
  ];
  if (job.skipped_images > 0) {
    parts.push(`${formatCount(job.skipped_images)} já estavam no índice`);
  }
  if (job.failed_images > 0) {
    parts.push(failedText(job.failed_images));
  }
  return parts.join(" · ");
}

/** `3.204 indexadas, 1.200 já estavam, 3 falharam.` */
export function finishedCounters(job: Job): string {
  const parts = [
    job.processed_images === 1
      ? "1 indexada"
      : `${formatCount(job.processed_images)} indexadas`,
  ];
  if (job.skipped_images > 0) {
    parts.push(`${formatCount(job.skipped_images)} já estavam`);
  }
  if (job.failed_images > 0) {
    parts.push(failedText(job.failed_images));
  }
  return `${parts.join(", ")}.`;
}

function failedText(count: number): string {
  return count === 1 ? "1 falhou" : `${formatCount(count)} falharam`;
}

/** What a job walks: `o disco inteiro`, or how many folders. */
export function scopeText(job: Pick<Job, "scopes">): string {
  return job.scopes.length === 0
    ? "o disco inteiro"
    : foldersCountText(job.scopes.length);
}

function atText(finished: Date | null, now: Date): string {
  if (finished === null) {
    return "";
  }
  const day = dayText(finished, now);
  return day === "hoje"
    ? ` às ${timeText(finished)}`
    : ` em ${day} às ${timeText(finished)}`;
}

export function jobPanel(job: Job, context: PanelContext): Panel {
  const now = context.now ?? new Date();
  switch (job.status) {
    case "pending":
      return {
        tone: "active",
        title: "Na fila.",
        lines: [
          context.runningElsewhere !== null
            ? `Começa quando a indexação do ${context.runningElsewhere} terminar.`
            : "Começa em instantes.",
        ],
        diagnostic: null,
        bar: null,
        actions: ["cancel"],
      };
    case "running":
      return runningPanel(job, context, now);
    case "completed": {
      const failed = job.failed_images > 0;
      return {
        tone: failed ? "attention" : "done",
        title: `Concluída${atText(instant(job.finished_at), now)}`,
        lines: [
          finishedCounters(job),
          ...(failed ? ["Os arquivos que falharam estão no log."] : []),
        ],
        diagnostic: null,
        bar: null,
        actions: failed ? ["logs"] : [],
      };
    }
    case "cancelled": {
      const checkpoint = job.last_processed_relative_path;
      return {
        tone: "done",
        title:
          checkpoint === null
            ? "Cancelada antes do primeiro arquivo."
            : `Cancelada — parou em ${cutLeft(checkpoint)}.`,
        lines: ["O que foi indexado fica, e a próxima indexação pula."],
        diagnostic: null,
        bar: null,
        actions: ["again"],
      };
    }
    case "failed":
      if (!context.connected) {
        return {
          tone: "attention",
          title: `A indexação parou porque o ${context.label} foi desconectado.`,
          lines: ["O que foi indexado fica."],
          diagnostic: null,
          bar: null,
          actions: ["again"],
        };
      }
      return {
        tone: "attention",
        title: "A indexação falhou.",
        lines: [],
        diagnostic: job.error_message,
        bar: null,
        actions: ["logs", "again"],
      };
  }
}

function runningPanel(job: Job, context: PanelContext, now: Date): Panel {
  if (job.cancel_requested) {
    return {
      tone: "active",
      title: "Cancelando…",
      lines: ["O lote em andamento termina primeiro."],
      diagnostic: null,
      bar: null,
      actions: [],
    };
  }
  const nothingYet =
    job.discovered_files === 0 &&
    job.processed_images === 0 &&
    job.skipped_images === 0 &&
    job.failed_images === 0;
  if (nothingYet) {
    return {
      tone: "active",
      title: `Lendo a lista de arquivos do ${context.label}…`,
      lines: [
        "Num disco grande, isto leva alguns minutos, e nada aparece até terminar.",
      ],
      diagnostic: null,
      bar: { kind: "indeterminate" },
      actions: ["cancel"],
    };
  }
  const lines = [runningCounters(job)];
  if (job.last_processed_relative_path !== null) {
    lines.push(`Último salvo: ${cutLeft(job.last_processed_relative_path)}`);
  }
  const barText = job.scopes.length === 0 ? diskBarText(context.device, now) : null;
  const ratio =
    context.device.last_scan_file_count !== null &&
    context.device.last_scan_file_count > 0
      ? Math.min(1, context.device.indexed_images / context.device.last_scan_file_count)
      : 0;
  return {
    tone: "active",
    title: `Indexando ${scopeText(job)}`,
    lines,
    diagnostic: null,
    bar: barText !== null ? { kind: "share", ratio, text: barText } : null,
    actions: ["cancel"],
  };
}

/** The note of section 5.8: the executor restarted with this job in hand. It
 * stays until the job changes state -- the reaper gives it back to the queue
 * 120 s after the last heartbeat (`job_stale_timeout`), and the executor's
 * new `running` comes before that. */
export const RESTART_NOTE =
  "O executor da indexação reiniciou. O job volta à fila sozinho, até 2 minutos " +
  "depois do último sinal.";

/** The key of a job's run: the reaper's requeue counts an attempt. */
export function runKey(job: Pick<Job, "id" | "attempts">): string {
  return `${job.id}#${job.attempts}`;
}
