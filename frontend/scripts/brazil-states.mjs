// The outline of Brazil's 27 federative units, the map's background without a
// network (RFC-040 section 5.2).
//
//     npm run map:outline
//
// Run once, with a network, and the result is versioned: the build downloads
// nothing. From Natural Earth, 1:10m, "Admin 1 - States, Provinces", which is in
// the public domain, as this project is (CC0): filtered to Brazil, simplified
// by mapshaper -- which simplifies the border two states share once, so no gap
// opens between neighbours -- and written with three decimal places. mapshaper
// runs through `npm exec`, at a fixed version, and is not a dependency.
//
// Into `src/map/brazil-states.json`: a FeatureCollection, one feature per line,
// with `name` and `sigla`, and a `source` member that says where it came from --
// the URL, the version, the SHA-256 and the size of what was downloaded -- and
// what was done to it. At most 200 KB, a choice of the RFC.

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { inflateRawSync } from "node:zlib";

const SOURCE_URL =
  "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip";
const LAYER = "ne_10m_admin_1_states_provinces";
/** Published 2026-10-07: two weeks before this was first run. */
const MAPSHAPER = "mapshaper@0.7.80";
const COMMANDS = [
  "-target",
  LAYER,
  "-filter",
  'adm0_a3 == "BRA"',
  "-filter-fields",
  "name,postal",
  "-rename-fields",
  "sigla=postal",
  "-simplify",
  "15%",
  "keep-shapes",
];
const OUTPUT = fileURLToPath(new URL("../src/map/brazil-states.json", import.meta.url));
export const LIMIT_BYTES = 200 * 1024;
export const UNITS = 27;

/** npm's own command line, run by this Node with no shell: the filter keeps its
 * spaces and its quotes on every platform. */
function npmCli() {
  const candidates = [
    process.env.npm_execpath,
    join(dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js"),
    join(
      dirname(process.execPath),
      "..",
      "lib",
      "node_modules",
      "npm",
      "bin",
      "npm-cli.js",
    ),
  ];
  const found = candidates.find(
    (file) => file !== undefined && file.endsWith(".js") && existsSync(file),
  );
  if (found === undefined) {
    throw new Error("npm's npm-cli.js was not found beside this Node");
  }
  return found;
}

/** One file of a zip, without a library: the central directory, then the
 * local header, then the data, stored or deflated. */
function zipEntry(zip, name) {
  let end = -1;
  for (let at = zip.length - 22; at >= Math.max(0, zip.length - 65_557); at -= 1) {
    if (zip.readUInt32LE(at) === 0x06054b50) {
      end = at;
      break;
    }
  }
  if (end < 0) {
    throw new Error("The download is not a zip");
  }
  let entry = zip.readUInt32LE(end + 16);
  for (let index = zip.readUInt16LE(end + 10); index > 0; index -= 1) {
    const nameLength = zip.readUInt16LE(entry + 28);
    const entryName = zip.toString("utf8", entry + 46, entry + 46 + nameLength);
    if (entryName === name) {
      const method = zip.readUInt16LE(entry + 10);
      const size = zip.readUInt32LE(entry + 20);
      const local = zip.readUInt32LE(entry + 42);
      const start =
        local + 30 + zip.readUInt16LE(local + 26) + zip.readUInt16LE(local + 28);
      const data = zip.subarray(start, start + size);
      return method === 0 ? data : inflateRawSync(data);
    }
    entry +=
      46 + nameLength + zip.readUInt16LE(entry + 30) + zip.readUInt16LE(entry + 32);
  }
  return null;
}

/** The text of the file: the source, then one feature per line. */
export function render(source, features) {
  const lines = features.map((feature) => JSON.stringify(feature));
  return (
    `{"type":"FeatureCollection","source":${JSON.stringify(source)},"features":[\n` +
    `${lines.join(",\n")}\n]}\n`
  );
}

async function main() {
  const work = mkdtempSync(join(tmpdir(), "brazil-states-"));
  try {
    console.log(`Downloading ${SOURCE_URL}`);
    const response = await fetch(SOURCE_URL);
    if (!response.ok) {
      throw new Error(`${SOURCE_URL}: ${response.status}`);
    }
    const zip = Buffer.from(await response.arrayBuffer());
    const sha256 = createHash("sha256").update(zip).digest("hex");
    const version =
      zipEntry(zip, `${LAYER}.VERSION.txt`)?.toString("utf8").trim() ?? null;
    const archive = join(work, `${LAYER}.zip`);
    writeFileSync(archive, zip);
    console.log(`${zip.length} bytes, SHA-256 ${sha256}, Natural Earth ${version}`);

    const simplified = join(work, "brazil.json");
    const run = spawnSync(
      process.execPath,
      [
        npmCli(),
        "exec",
        "--yes",
        `--package=${MAPSHAPER}`,
        "--",
        "mapshaper",
        "-i",
        archive,
        ...COMMANDS,
        "-o",
        simplified,
        "format=geojson",
        "precision=0.001",
      ],
      { encoding: "utf8", stdio: ["ignore", "inherit", "inherit"] },
    );
    if (run.status !== 0) {
      throw new Error(`mapshaper exited with ${run.status}`);
    }

    const collection = JSON.parse(readFileSync(simplified, "utf8"));
    const features = collection.features.map((feature) => ({
      type: "Feature",
      properties: { name: feature.properties.name, sigla: feature.properties.sigla },
      geometry: feature.geometry,
    }));
    if (features.length !== UNITS) {
      throw new Error(`${features.length} units, not ${UNITS}`);
    }
    const source = {
      dataset: "Natural Earth 1:10m Cultural Vectors, Admin 1 - States, Provinces",
      version,
      url: SOURCE_URL,
      sha256,
      bytes: zip.length,
      lastModified: response.headers.get("last-modified"),
      license: "Public domain: https://www.naturalearthdata.com/about/terms-of-use/",
      tool: MAPSHAPER,
      commands: [...COMMANDS, "-o", "format=geojson", "precision=0.001"].join(" "),
      generated: new Date().toISOString().slice(0, 10),
    };
    const text = render(source, features);
    const bytes = Buffer.byteLength(text);
    if (bytes > LIMIT_BYTES) {
      throw new Error(`${bytes} bytes, above the ${LIMIT_BYTES} of the RFC`);
    }
    writeFileSync(OUTPUT, text);
    console.log(`${OUTPUT}: ${features.length} units, ${bytes} bytes`);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main();
}
