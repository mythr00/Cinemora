import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

/**
 * Phase 9 frontend test suite (Vitest + Testing Library + jsdom).
 *
 * Tests live under tests/ and mock lib/api at the module boundary.
 * next/navigation and next/image are aliased to small stubs in
 * tests/stubs/ — the components under test only need pathname /
 * router / params hooks and a plain <img>, never the Next runtime.
 */
export default defineConfig({
  resolve: {
    alias: [
      { find: /^next\/navigation$/, replacement: path.join(root, "tests/stubs/next-navigation.ts") },
      { find: /^next\/image$/, replacement: path.join(root, "tests/stubs/next-image.tsx") },
      { find: /^@\/(.*)$/, replacement: path.join(root, "$1") },
    ],
  },
  esbuild: {
    jsx: "automatic",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: [path.join(root, "tests/setup.ts")],
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
