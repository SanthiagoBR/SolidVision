import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores(["dist/", "dist-electron/", "release/", "src/api/schema.d.ts"]),
  js.configs.recommended,
  tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    extends: [reactHooks.configs.flat.recommended],
  },
  {
    files: ["electron/**/*.ts", "scripts/**/*.mjs", "*.config.{mjs,mts}"],
    languageOptions: { globals: globals.node },
  },
  {
    // electron-builder reads its configuration with `require` (RFC-041 7.1).
    files: ["**/*.cjs"],
    languageOptions: { globals: globals.node, sourceType: "commonjs" },
    rules: { "@typescript-eslint/no-require-imports": "off" },
  },
]);
