import { describe, expect, it } from "vitest";

import {
  BAD_FILTERS,
  errorMessage,
  FAILED,
  MAX_UPLOAD_BYTES,
  NO_ANSWER,
  NOT_IN_INDEX,
  NOT_INDEXED,
  TOO_LARGE,
  UNREADABLE,
  uploadRefusal,
  WRONG_TYPE,
} from "./messages";

describe("the sentence of each failure, by status and by route", () => {
  it("400 is the filters on a search, and the photo on an upload", () => {
    expect(errorMessage("search", 400, "A search radius of 200 m is below…")).toBe(
      BAD_FILTERS,
    );
    expect(errorMessage("similar", 400)).toBe(BAD_FILTERS);
    expect(errorMessage("upload", 400)).toBe(UNREADABLE);
  });

  it("404 and 409 of /similar and of the detail", () => {
    expect(errorMessage("similar", 404)).toBe(NOT_IN_INDEX);
    expect(errorMessage("detail", 404)).toBe(NOT_IN_INDEX);
    expect(errorMessage("similar", 409)).toBe(NOT_INDEXED);
  });

  it("409 and 410 of the reveal name the disk and the folder", () => {
    const where = { label: "HD3", folder: "F:\\fotos\\2018\\junho" };

    expect(errorMessage("reveal", 409, null, where)).toBe(
      "O HD3 não está conectado. Conecte o disco e tente de novo.",
    );
    expect(errorMessage("reveal", 410, null, where)).toBe(
      "O arquivo não está mais em F:\\fotos\\2018\\junho.",
    );
  });

  it("413 and 415 of the upload", () => {
    expect(errorMessage("upload", 413)).toBe(TOO_LARGE);
    expect(errorMessage("upload", 415)).toBe(WRONG_TYPE);
  });

  it("503 is the API's own line: it names the model and what to do", () => {
    const line =
      "The CLIP model in runtime\\models is incomplete: run vendor_models.py";

    expect(errorMessage("search", 503, line)).toBe(line);
    expect(errorMessage("upload", 503, line)).toBe(line);
  });

  it("no answer at all, and anything else", () => {
    expect(errorMessage("search", 0)).toBe(NO_ANSWER);
    expect(errorMessage("search", 500)).toBe(FAILED);
  });
});

describe("a photo refused before it is sent", () => {
  it("is not a JPEG or a PNG", () => {
    expect(uploadRefusal({ type: "image/webp", size: 10 })).toBe(WRONG_TYPE);
    expect(uploadRefusal({ type: "", size: 10 })).toBe(WRONG_TYPE);
  });

  it("is above 50 MB", () => {
    expect(uploadRefusal({ type: "image/jpeg", size: MAX_UPLOAD_BYTES + 1 })).toBe(
      TOO_LARGE,
    );
    expect(uploadRefusal({ type: "image/png", size: MAX_UPLOAD_BYTES })).toBeNull();
  });
});
