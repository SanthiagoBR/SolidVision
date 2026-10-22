// The build's Content-Security-Policy (RFC-037 section 5.2, RFC-038 section 2.6,
// RFC-040 section 2.8): `blob:` in `img-src`, for the preview of a dropped
// photo, the OpenStreetMap's tiles beside it, and the rest as it was. Here,
// beside the other test of a build-time file, because the policy is written in
// `vite.config.mts`.

import { expect, it } from "vitest";

import { CONTENT_SECURITY_POLICY } from "../vite.config.mts";

it("allows blob: images and the tiles' host, and nothing else changed", () => {
  expect(CONTENT_SECURITY_POLICY.split("; ")).toEqual([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: blob: https://tile.openstreetmap.org",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
  ]);
});
