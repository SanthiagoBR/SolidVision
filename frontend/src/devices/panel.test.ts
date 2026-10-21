import { describe, expect, it } from "vitest";

import type { Job } from "../api/client";
import { cardJobText, cardPhotosText, connectionText } from "./card";
import { labelProblem, suggestLabel } from "./labels";
import {
  cancelErrorText,
  foldersErrorText,
  jobConflictText,
  jobErrorText,
  NO_ANSWER,
  registerErrorText,
  renameErrorText,
} from "./messages";
import { jobPanel, type PanelContext } from "./panel";

const NOW = new Date(2026, 9, 26, 15, 0);
const TODAY_1402 = new Date(2026, 9, 26, 14, 2).toISOString();

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "00000000-0000-0000-0000-000000000039",
    device_id: "00000000-0000-0000-0000-0000000000d2",
    status: "running",
    scopes: [],
    created_at: null,
    started_at: null,
    finished_at: null,
    discovered_files: 0,
    discovery_complete: false,
    processed_images: 0,
    skipped_images: 0,
    failed_images: 0,
    last_processed_relative_path: null,
    cancel_requested: false,
    attempts: 0,
    error_message: null,
    ...overrides,
  };
}

function context(overrides: Partial<PanelContext> = {}): PanelContext {
  return {
    label: "HD2",
    connected: true,
    device: { indexed_images: 0, last_scan_file_count: null, last_scan_at: null },
    runningElsewhere: null,
    now: NOW,
    ...overrides,
  };
}

describe("the panel of the job (section 5.7)", () => {
  it("pending: waits for another disk, or begins in a moment", () => {
    const pending = job({ status: "pending" });

    expect(jobPanel(pending, context())).toMatchObject({
      title: "Na fila.",
      lines: ["Começa em instantes."],
      actions: ["cancel"],
    });
    expect(jobPanel(pending, context({ runningElsewhere: "HD3" })).lines).toEqual([
      "Começa quando a indexação do HD3 terminar.",
    ]);
  });

  it("running with everything at zero: the listing, which gives no sign", () => {
    expect(jobPanel(job(), context())).toEqual({
      tone: "active",
      title: "Lendo a lista de arquivos do HD2…",
      lines: [
        "Num disco grande, isto leva alguns minutos, e nada aparece até terminar.",
      ],
      diagnostic: null,
      bar: { kind: "indeterminate" },
      actions: ["cancel"],
    });
  });

  it("running: the counters, the last file, and no bar for some folders", () => {
    const running = job({
      scopes: ["2018", "2019"],
      discovered_files: 4_800,
      processed_images: 3_204,
      skipped_images: 1_200,
      failed_images: 3,
      last_processed_relative_path: "2018/junho/DJI_0042.JPG",
    });

    expect(jobPanel(running, context())).toMatchObject({
      title: "Indexando 2 pastas",
      lines: [
        "3.204 indexadas agora · 1.200 já estavam no índice · 3 falharam",
        "Último salvo: …/junho/DJI_0042.JPG",
      ],
      bar: null,
      actions: ["cancel"],
    });
  });

  it("running the whole disk: the bar of the device, with the date of its scan", () => {
    const today = new Date(2026, 9, 26, 9, 0).toISOString();
    const panel = jobPanel(
      job({ processed_images: 12_000, discovered_files: 12_512 }),
      context({
        device: {
          indexed_images: 12_000,
          last_scan_file_count: 48_210,
          last_scan_at: today,
        },
      }),
    );

    expect(panel.title).toBe("Indexando o disco inteiro");
    expect(panel.bar).toEqual({
      kind: "share",
      ratio: 12_000 / 48_210,
      text: "24% do disco · 12.000 de 48.210 no scan de hoje",
    });
  });

  it("running the whole disk before its denominator: no bar", () => {
    const panel = jobPanel(
      job({ processed_images: 8, discovered_files: 512 }),
      context(),
    );

    expect(panel.bar).toBeNull();
  });

  it("cancel requested: Cancelando…, until the job ends", () => {
    expect(
      jobPanel(job({ cancel_requested: true, processed_images: 40 }), context()),
    ).toMatchObject({
      title: "Cancelando…",
      lines: ["O lote em andamento termina primeiro."],
      actions: [],
    });
  });

  it("completed: when, and the counters; the log only when something failed", () => {
    const done = job({
      status: "completed",
      finished_at: TODAY_1402,
      processed_images: 3_204,
      skipped_images: 1_200,
    });

    expect(jobPanel(done, context())).toMatchObject({
      tone: "done",
      title: "Concluída às 14:02",
      lines: ["3.204 indexadas, 1.200 já estavam."],
      actions: [],
    });
    expect(jobPanel({ ...done, failed_images: 3 }, context())).toMatchObject({
      tone: "attention",
      lines: [
        "3.204 indexadas, 1.200 já estavam, 3 falharam.",
        "Os arquivos que falharam estão no log.",
      ],
      actions: ["logs"],
    });
  });

  it("completed on another day says the day", () => {
    const done = job({
      status: "completed",
      finished_at: new Date(2026, 8, 14, 22, 10).toISOString(),
    });

    expect(jobPanel(done, context()).title).toBe("Concluída em 14/09 às 22:10");
  });

  it("cancelled: where it stopped, and Indexar de novo", () => {
    expect(
      jobPanel(
        job({ status: "cancelled", last_processed_relative_path: "2018/IMG_2201.JPG" }),
        context(),
      ),
    ).toMatchObject({
      title: "Cancelada — parou em 2018/IMG_2201.JPG.",
      lines: ["O que foi indexado fica, e a próxima indexação pula."],
      actions: ["again"],
    });
    expect(jobPanel(job({ status: "cancelled" }), context()).title).toBe(
      "Cancelada antes do primeiro arquivo.",
    );
  });

  it("failed with the disk away now: it stopped because the disk left", () => {
    const failed = job({
      status: "failed",
      error_message: "The device was disconnected before the scan finished",
    });

    expect(jobPanel(failed, context({ connected: false }))).toMatchObject({
      title: "A indexação parou porque o HD2 foi desconectado.",
      lines: ["O que foi indexado fica."],
      diagnostic: null,
      actions: ["again"],
    });
  });

  it("failed: the error_message as the diagnosis, the logs and Indexar de novo", () => {
    const failed = job({ status: "failed", error_message: "Boom: disk error" });

    expect(jobPanel(failed, context())).toMatchObject({
      title: "A indexação falhou.",
      diagnostic: "Boom: disk error",
      actions: ["logs", "again"],
    });
  });
});

describe("the 409 of POST /jobs, read again from /devices (section 2.5)", () => {
  const device = (connected: boolean, busy: boolean) => ({
    label: "HD2",
    connected,
    active_job: busy
      ? { id: "00000000-0000-0000-0000-000000000040", status: "running" as const }
      : null,
  });

  it("says the disk left", () => {
    expect(jobConflictText(device(false, false))).toBe(
      "O HD2 foi desconectado. Conecte o disco e tente de novo.",
    );
  });

  it("says the disk is busy", () => {
    expect(jobConflictText(device(true, true))).toBe(
      "O HD2 já tem uma indexação em andamento.",
    );
  });

  it("says it could not begin when neither shows", () => {
    expect(jobConflictText(device(true, false))).toBe(
      "Não deu para começar a indexação. Tente de novo.",
    );
    expect(jobConflictText(undefined)).toBe(
      "Esse dispositivo não está mais registrado.",
    );
  });

  it("and the other statuses of the table", () => {
    expect(jobErrorText(400, "HD2")).toBe(
      "Uma das pastas escolhidas não está mais no HD2.",
    );
    expect(jobErrorText(0, "HD2")).toBe(NO_ANSWER);
    expect(foldersErrorText(409, "HD2", "junho")).toBeNull();
    expect(foldersErrorText(400, "HD2", "junho")).toBe(
      "A pasta junho não está mais no HD2.",
    );
    expect(registerErrorText(409, "F:/")).toBe(
      "O F: foi desconectado. Conecte o disco e procure de novo.",
    );
    expect(registerErrorText(400, "F:/")).toBe("Não deu para adicionar esse disco.");
    expect(renameErrorText(422)).toBe("O nome não pode ficar em branco.");
    expect(renameErrorText(404)).toBe("Esse dispositivo não está mais registrado.");
    expect(cancelErrorText(409)).toBeNull();
  });
});

describe("the label (section 2.8)", () => {
  it("suggests the volume's own, or HD and the smallest number nobody uses", () => {
    expect(suggestLabel("Seagate Backup", ["HD1"])).toBe("Seagate Backup");
    expect(suggestLabel(null, ["HD1", "hd2", "HD4"])).toBe("HD3");
    expect(suggestLabel("   ", [])).toBe("HD1");
    expect(suggestLabel("HD1", ["hd1 "])).toBe("HD2");
  });

  it("refuses an empty one", () => {
    expect(labelProblem("   ", ["HD1"])).toEqual({ kind: "empty" });
  });

  it("refuses a repeated one, case and the spaces of the ends aside", () => {
    expect(labelProblem(" hd2 ", ["HD1", "HD2"])).toEqual({
      kind: "repeated",
      text: "Já existe um HD2",
    });
    expect(labelProblem("HD3", ["HD1", "HD2"])).toBeNull();
  });

  it("lets a rename keep its own label, given the others", () => {
    expect(labelProblem("hd2", ["HD1"])).toBeNull();
  });
});

describe("the card (section 5.2)", () => {
  const active = (status: "pending" | "running") => ({
    id: "00000000-0000-0000-0000-000000000039",
    status,
  });

  it("says where the disk is, or that it is away", () => {
    expect(connectionText({ connected: true, mount_point: "F:/" })).toBe("F:");
    expect(connectionText({ connected: false, mount_point: null })).toBe(
      "desconectado",
    );
  });

  it("counts the photos, and calls none and no job never indexed", () => {
    expect(cardPhotosText({ indexed_images: 24_104, active_job: null })).toBe(
      "24.104 fotos indexadas",
    );
    expect(cardPhotosText({ indexed_images: 1, active_job: null })).toBe(
      "1 foto indexada",
    );
    expect(cardPhotosText({ indexed_images: 0, active_job: null })).toBe(
      "nunca indexado",
    );
    expect(cardPhotosText({ indexed_images: 0, active_job: active("pending") })).toBe(
      "0 fotos indexadas",
    );
  });

  it("says the job, with the counter once GET /jobs/{id} answers", () => {
    expect(cardJobText(null, undefined)).toBeNull();
    expect(cardJobText(active("pending"), undefined)).toBe("Na fila");
    expect(cardJobText(active("running"), undefined)).toBe("Indexando");
    expect(cardJobText(active("running"), job({ processed_images: 3_204 }))).toBe(
      "Indexando · 3.204 agora",
    );
    expect(cardJobText(active("running"), job({ cancel_requested: true }))).toBe(
      "Cancelando…",
    );
  });
});
