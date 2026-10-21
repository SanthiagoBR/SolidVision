import { describe, expect, it } from "vitest";

import {
  copyablePath,
  copyLabel,
  displayPath,
  fileName,
  folderOf,
  parentFolder,
} from "./paths";

const here = {
  relative_path: "fotos/2018/junho/DJI_0042.JPG",
  absolute_path: "F:/fotos/2018/junho/DJI_0042.JPG",
  device: { label: "HD3", connected: true },
};
const away = {
  ...here,
  absolute_path: null,
  device: { label: "HD3", connected: false },
};

describe("the paths of a photo", () => {
  it("names the file by the last segment, extension and all", () => {
    expect(fileName(here.relative_path)).toBe("DJI_0042.JPG");
    expect(folderOf(here.relative_path)).toBe("fotos/2018/junho");
    expect(folderOf("DJI_0042.JPG")).toBe("");
  });

  it("shows the absolute path with the disk here, in Windows form", () => {
    expect(displayPath(here)).toBe("F:\\fotos\\2018\\junho\\DJI_0042.JPG");
    expect(copyablePath(here)).toBe("F:\\fotos\\2018\\junho\\DJI_0042.JPG");
    expect(copyLabel(here)).toBe("Copiar caminho");
  });

  it("shows the disk and the path inside it, with the disk away", () => {
    expect(displayPath(away)).toBe("HD3 · fotos\\2018\\junho\\DJI_0042.JPG");
    expect(copyablePath(away)).toBe("fotos\\2018\\junho\\DJI_0042.JPG");
    expect(copyLabel(away)).toBe("Copiar caminho no HD3");
  });

  it("names the folder a file was in", () => {
    expect(parentFolder(here.absolute_path)).toBe("F:\\fotos\\2018\\junho");
  });
});
