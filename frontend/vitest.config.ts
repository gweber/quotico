/**
 * frontend/vitest.config.ts
 *
 * Purpose:
 * Vitest configuration merged with the existing Vite setup.
 */

import { defineConfig, mergeConfig } from "vitest/config";
import baseConfig from "./vite.config";

export default mergeConfig(
  baseConfig,
  defineConfig({
    test: {
      globals: true,
      environment: "jsdom",
      include: ["src/**/*.spec.ts", "src/**/*.test.ts"],
      exclude: ["dist", "node_modules"],
    },
  })
);

