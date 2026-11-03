import { describe, expect, it } from "vitest";

import { cardMissingText, purgeConfirmText, purgeDisabled } from "./card";
import { purgeErrorText } from "./messages";

const NOW = new Date(2026, 9, 31, 15, 0);
const MARCH = new Date(2026, 2, 12, 10, 30).toISOString();

describe("cardMissingText (RFC-046 section 5.8)", () => {
  it("says nothing for a disk with nothing missing", () => {
    expect(cardMissingText({ missing_images: 0, missing_since: null }, NOW)).toBeNull();
  });

  it("says how many and since which scan", () => {
    expect(cardMissingText({ missing_images: 312, missing_since: MARCH }, NOW)).toBe(
      "312 fotos não encontradas no scan de 12/03",
    );
  });

  it("groups the thousands and speaks of one photo in the singular", () => {
    expect(cardMissingText({ missing_images: 1_268, missing_since: MARCH }, NOW)).toBe(
      "1.268 fotos não encontradas no scan de 12/03",
    );
    expect(cardMissingText({ missing_images: 1, missing_since: MARCH }, NOW)).toBe(
      "1 foto não encontrada no scan de 12/03",
    );
  });

  it("names the year when it is not this one, and today as today", () => {
    const lastYear = new Date(2025, 11, 31, 23, 0).toISOString();
    const today = new Date(2026, 9, 31, 9, 30).toISOString();

    expect(cardMissingText({ missing_images: 3, missing_since: lastYear }, NOW)).toBe(
      "3 fotos não encontradas no scan de 31/12/2025",
    );
    expect(cardMissingText({ missing_images: 3, missing_since: today }, NOW)).toBe(
      "3 fotos não encontradas no scan de hoje",
    );
  });

  it("says nothing without a readable date", () => {
    expect(cardMissingText({ missing_images: 3, missing_since: null }, NOW)).toBeNull();
    expect(
      cardMissingText({ missing_images: 3, missing_since: "not a date" }, NOW),
    ).toBeNull();
  });
});

describe("purgeConfirmText", () => {
  it("asks with the number and the disk, as the RFC writes it", () => {
    expect(purgeConfirmText({ label: "HD3", missing_images: 312 })).toBe(
      "Remover 312 fotos do HD3 do índice? Os arquivos não estão mais no disco. " +
        "Se voltarem, serão indexados de novo.",
    );
  });

  it("asks about one photo in the singular", () => {
    expect(purgeConfirmText({ label: "HD3", missing_images: 1 })).toBe(
      "Remover 1 foto do HD3 do índice? O arquivo não está mais no disco. " +
        "Se voltar, será indexado de novo.",
    );
  });
});

describe("purgeDisabled", () => {
  it("waits while the disk has a job, pending or running", () => {
    expect(purgeDisabled({ active_job: null })).toBe(false);
    expect(purgeDisabled({ active_job: { id: "j", status: "pending" } })).toBe(true);
    expect(purgeDisabled({ active_job: { id: "j", status: "running" } })).toBe(true);
  });
});

describe("purgeErrorText", () => {
  it("names the job for a 409, and the usual sentences otherwise", () => {
    expect(purgeErrorText(409, "HD3")).toBe(
      "O HD3 tem uma indexação em andamento. Tente quando ela terminar.",
    );
    expect(purgeErrorText(404, "HD3")).toBe(
      "Esse dispositivo não está mais registrado.",
    );
    expect(purgeErrorText(0, "HD3")).toBe(
      "Sem resposta do SolidVision. Tente de novo em instantes.",
    );
    expect(purgeErrorText(500, "HD3")).toBe(
      "Não deu para remover as fotos do índice. Tente de novo.",
    );
  });
});
