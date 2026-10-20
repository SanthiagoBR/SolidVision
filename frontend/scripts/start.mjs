// `npm start`: the window over the build (RFC-037 section 4.2). It loads the URL
// of `ready`, and the API serves `dist/`: `npm run build` first.

import { runElectron } from "./electron.mjs";

const window = runElectron();
window.on("exit", (code) => process.exit(code ?? 0));
