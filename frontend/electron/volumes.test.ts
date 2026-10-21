import { describe, expect, it } from "vitest";

import {
  DBT_DEVICEARRIVAL,
  DBT_DEVICEREMOVECOMPLETE,
  GROUP_MS,
  volumeChangeOf,
  volumeChangesLine,
  VolumeMessages,
  type VolumeChanges,
} from "./volumes";

/** A `wParam` as `hookWindowMessage` gives it: 8 bytes, little-endian. */
function wParam(code: number): Uint8Array {
  const bytes = new Uint8Array(8);
  new DataView(bytes.buffer).setUint32(0, code, true);
  return bytes;
}

/** A timer the test advances by hand. */
class Clock {
  now = 0;
  private timers: { at: number; callback: () => void }[] = [];

  readonly schedule = (callback: () => void, afterMs: number) => {
    this.timers.push({ at: this.now + afterMs, callback });
  };

  advance(ms: number): void {
    this.now += ms;
    const due = this.timers.filter((timer) => timer.at <= this.now);
    this.timers = this.timers.filter((timer) => timer.at > this.now);
    due.forEach((timer) => timer.callback());
  }
}

describe("volumeChangeOf", () => {
  it("reads a volume arriving and one leaving", () => {
    expect(volumeChangeOf(wParam(DBT_DEVICEARRIVAL))).toBe("arrival");
    expect(volumeChangeOf(wParam(DBT_DEVICEREMOVECOMPLETE))).toBe("removal");
    expect(volumeChangeOf(DBT_DEVICEARRIVAL)).toBe("arrival");
  });

  it("ignores every other message, the device nodes of a mouse among them", () => {
    expect(volumeChangeOf(wParam(0x0007))).toBeNull(); // DBT_DEVNODES_CHANGED
    expect(volumeChangeOf(wParam(0x8001))).toBeNull(); // DBT_QUERYREMOVE
    expect(volumeChangeOf(wParam(0x8003))).toBeNull(); // DBT_DEVICEREMOVEPENDING
    expect(volumeChangeOf(new Uint8Array(2))).toBeNull();
  });

  it("reads the low 32 bits of a buffer that is a slice of a larger one", () => {
    const larger = new Uint8Array(16);
    new DataView(larger.buffer).setUint32(8, DBT_DEVICEREMOVECOMPLETE, true);

    expect(volumeChangeOf(larger.subarray(8))).toBe("removal");
  });
});

describe("VolumeMessages", () => {
  function setup() {
    const clock = new Clock();
    const reports: VolumeChanges[] = [];
    const messages = new VolumeMessages(clock.schedule, (changes) =>
      reports.push(changes),
    );
    return { clock, reports, messages };
  }

  it("tells the page once, 250 ms after the first message", () => {
    const { clock, reports, messages } = setup();

    expect(messages.receive(wParam(DBT_DEVICEARRIVAL))).toBe(true);
    clock.advance(GROUP_MS - 1);
    expect(reports).toEqual([]);
    clock.advance(1);

    expect(reports).toEqual([{ arrivals: 1, removals: 0 }]);
  });

  it("groups the volumes of one disk into one change", () => {
    const { clock, reports, messages } = setup();

    messages.receive(wParam(DBT_DEVICEARRIVAL));
    clock.advance(40);
    messages.receive(wParam(DBT_DEVICEARRIVAL));
    clock.advance(100);
    messages.receive(wParam(DBT_DEVICEREMOVECOMPLETE));
    clock.advance(GROUP_MS);

    expect(reports).toEqual([{ arrivals: 2, removals: 1 }]);
  });

  it("starts a new group after the last one was reported", () => {
    const { clock, reports, messages } = setup();

    messages.receive(wParam(DBT_DEVICEREMOVECOMPLETE));
    clock.advance(GROUP_MS);
    messages.receive(wParam(DBT_DEVICEARRIVAL));
    clock.advance(GROUP_MS);

    expect(reports).toEqual([
      { arrivals: 0, removals: 1 },
      { arrivals: 1, removals: 0 },
    ]);
  });

  it("ignores the other messages, and they start no group", () => {
    const { clock, reports, messages } = setup();

    expect(messages.receive(wParam(0x0007))).toBe(false);
    clock.advance(GROUP_MS * 4);

    expect(reports).toEqual([]);
  });

  it("writes a line without a drive letter", () => {
    const line = volumeChangesLine({ arrivals: 1, removals: 0 });

    expect(line).toContain("1 arrived, 0 removed");
    expect(line).not.toMatch(/[A-Z]:/);
  });
});
