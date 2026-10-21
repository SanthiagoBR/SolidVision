import { describe, expect, it } from "vitest";

import { volumeFacts, volumeRowKind } from "./dialog";

const volume = (deviceId: string | null, totalBytes: number | null) => ({
  device_id: deviceId,
  total_bytes: totalBytes,
});

describe("the rows of the dialog (section 5.4)", () => {
  it("shows a volume already registered as the device it is", () => {
    expect(volumeRowKind(volume("d2", 2_000_398_934_016))).toBe("registered");
    expect(volumeRowKind(volume("d2", null))).toBe("registered");
  });

  it("offers a volume that answered", () => {
    expect(volumeRowKind(volume(null, 2_000_398_934_016))).toBe("addable");
  });

  it("does not offer one whose capacity did not read: an empty card reader", () => {
    expect(volumeRowKind(volume(null, null))).toBe("silent");
  });

  it("says the label and the size, or that there is no label", () => {
    expect(
      volumeFacts({
        filesystem_label: "Seagate Backup",
        total_bytes: 2_000_398_934_016,
      }),
    ).toBe("Seagate Backup · 2 TB");
    expect(volumeFacts({ filesystem_label: null, total_bytes: null })).toBe(
      "sem rótulo",
    );
    expect(volumeFacts({ filesystem_label: "", total_bytes: 31_914_983_424 })).toBe(
      "sem rótulo · 32 GB",
    );
  });
});
