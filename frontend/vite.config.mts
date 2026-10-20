import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

// `npm run dev` sets API_PORT for the launcher it starts, and the proxy goes
// there; `npm run dev:web` expects a launcher on this port (RFC-037 4.2).
const apiPort = process.env.API_PORT ?? "8000";
const api = `http://127.0.0.1:${apiPort}`;

// The page talks to its own origin and nothing else. In the build only: Vite's
// development server injects an inline script for the refresh of the page.
// The map's tiles will widen `img-src` (RFC-040).
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "solidvision-content-security-policy",
    apply: "build",
    transformIndexHtml: () => [
      {
        tag: "meta",
        attrs: {
          "http-equiv": "Content-Security-Policy",
          content: CONTENT_SECURITY_POLICY,
        },
        injectTo: "head-prepend",
      },
    ],
  };
}

export default defineConfig({
  plugins: [react(), contentSecurityPolicy()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: { "/api": api, "/health": api },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "electron/**/*.test.ts", "scripts/**/*.test.mjs"],
  },
});
