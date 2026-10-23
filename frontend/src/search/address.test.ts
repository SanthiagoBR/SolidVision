import { describe, expect, it } from "vitest";

import {
  addressErrorText,
  FAILED,
  NO_ANSWER,
  SWITCHED_OFF,
  UNAVAILABLE,
} from "./address";

describe("what the address field says when the lookup fails (RFC-043)", () => {
  it("no answer at all is the SolidVision's, not the service's", () => {
    expect(addressErrorText(0)).toBe(NO_ANSWER);
  });

  it("503 is the online service, and points to the coordinates", () => {
    expect(addressErrorText(503)).toBe(UNAVAILABLE);
    expect(UNAVAILABLE).toContain("coordenadas");
  });

  it("403 is the setting, switched off since the field was drawn", () => {
    expect(addressErrorText(403)).toBe(SWITCHED_OFF);
  });

  it("anything else is a failure to try again", () => {
    expect(addressErrorText(500)).toBe(FAILED);
    expect(addressErrorText(-1)).toBe(FAILED);
  });
});
