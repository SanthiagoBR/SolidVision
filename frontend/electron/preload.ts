/**
 * The bridge between the pages and the window (RFC-037 section 6.1).
 *
 * To the interface, two things: `onLauncherEvent`, which passes on the
 * launcher's `child` and `address` events, and `openLogsFolder`. And
 * `reportHome`, the mark of the home screen painted with its numbers, which
 * section 6.5 has the page give. The interface works without any of it, as in
 * `npm run dev:web`. To the start page, `screen`, `onScreen` and `reopen`.
 *
 * A sandboxed preload is a plain script with a `require` of a few modules. No
 * `import` here: it would make this file a module, and the compiled file would
 * refer to an `exports` that a sandboxed preload does not have. The block keeps
 * the names out of the global scope.
 */

{
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const electron = require("electron") as typeof import("electron");
  const { contextBridge, ipcRenderer } = electron;

  const subscribe = (channel: string, callback: (payload: unknown) => void) => {
    const listener = (_event: unknown, payload: unknown) => callback(payload);
    ipcRenderer.on(channel, listener);
    return () => {
      ipcRenderer.removeListener(channel, listener);
    };
  };

  contextBridge.exposeInMainWorld("solidvision", {
    onLauncherEvent: (callback: (event: unknown) => void) =>
      subscribe("launcher-event", callback),
    openLogsFolder: () => ipcRenderer.send("open-logs-folder"),
    reportHome: (report: unknown) => ipcRenderer.send("home-ready", report),
    screen: (): Promise<unknown> => ipcRenderer.invoke("screen"),
    onScreen: (callback: (screen: unknown) => void) => subscribe("screen", callback),
    reopen: () => ipcRenderer.send("reopen"),
  });
}
