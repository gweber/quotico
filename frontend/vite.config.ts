import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:4201",
      },
      "/health": {
        target: "http://localhost:4201",
      },
      "/ws": {
        target: "ws://localhost:4201",
        ws: true,
      },
    },
  },
});
