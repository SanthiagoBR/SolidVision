// `npm run dev`: Vite on localhost:5173, and the window, which starts the
// launcher and, on `ready`, loads Vite's page instead of the API's
// (RFC-037 section 4.2).
//
// The proxy sends /api and /health to 127.0.0.1:API_PORT, 8000 unless the
// environment says otherwise, and the launcher this window starts gets the
// same API_PORT: it serves exactly that port or refuses to start, saying why
// (RFC-036 section 5.4). Only this launcher: `npm start` and a launcher in a
// terminal still choose their own.

import { fileURLToPath } from "node:url";

import { createServer } from "vite";

import { runElectron } from "./electron.mjs";

const apiPort = process.env.API_PORT ?? "8000";
process.env.API_PORT = apiPort;

const server = await createServer({
  configFile: fileURLToPath(new URL("../vite.config.mts", import.meta.url)),
});
await server.listen();
server.printUrls();
const url = server.resolvedUrls?.local[0] ?? "http://localhost:5173/";

const window = runElectron({ SOLIDVISION_DEV_SERVER_URL: url, API_PORT: apiPort });
window.on("exit", async (code) => {
  await server.close();
  process.exit(code ?? 0);
});
process.on("SIGINT", () => window.kill());
