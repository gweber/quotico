import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // 4-level dark surface system (verified ≥4.5:1 contrast with text)
        surface: {
          0: "#0a0e17", // Darkest — page background
          1: "#111827", // Cards, main content areas
          2: "#1e293b", // Elevated surfaces (BetSlip, modals)
          3: "#334155", // Hover states, borders
        },
        // Accent colors — verified ≥4.5:1 contrast on surface-0/1
        primary: {
          DEFAULT: "#22c55e", // Green — main actions, wins
          hover: "#16a34a",
          muted: "#166534",
        },
        secondary: {
          DEFAULT: "#3b82f6", // Blue — links, info
          hover: "#2563eb",
          muted: "#1e40af",
        },
        // Semantic colors
        danger: {
          DEFAULT: "#ef4444", // Losses, errors
          hover: "#dc2626",
          muted: "#991b1b",
        },
        warning: {
          DEFAULT: "#f59e0b", // Draws, caution
          hover: "#d97706",
        },
        // Text colors — verified ≥4.5:1 on surface-0 through surface-2
        text: {
          primary: "#f8fafc",   // slate-50 — main text
          secondary: "#94a3b8", // slate-400 — secondary info
          muted: "#96aed0",     // +~50% brightness vs prior muted for better readability
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
      // Consistent spacing for odds/data-dense layout
      spacing: {
        "touch": "2.75rem", // 44px — WCAG minimum touch target
      },
      borderRadius: {
        card: "0.75rem",
      },
      ringWidth: {
        focus: "2px",
      },
      ringColor: {
        focus: "#22c55e",
      },
    },
  },
  plugins: [],
} satisfies Config;
