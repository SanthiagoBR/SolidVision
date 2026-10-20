// The half of the drift check that needs Node (RFC-037 section 4.3): schema.d.ts
// is what openapi-typescript generates from the versioned openapi.json. The
// other half, in pytest, holds openapi.json to the code.

import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

import { generateSchemaTypes, SCHEMA_TYPES } from "./schema-types.mjs";

it("schema.d.ts is what openapi-typescript generates from openapi.json", async () => {
  // A checkout with core.autocrlf may hold the file with CRLF.
  const written = readFileSync(SCHEMA_TYPES, "utf8").replace(/\r\n/g, "\n");
  const generated = await generateSchemaTypes();

  expect(
    written === generated,
    "schema.d.ts is not what openapi.json gives: run `npm run api:types`",
  ).toBe(true);
});
