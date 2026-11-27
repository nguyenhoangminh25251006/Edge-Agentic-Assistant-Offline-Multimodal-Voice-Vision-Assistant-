/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: { 950: "#0a1a2e", 900: "#0f243e", 800: "#163252", 700: "#1f3f63" },
        canvas: "#e9edf2",
        panel: "#f3f5f8",
        line: { DEFAULT: "#d4dae2", strong: "#b9c2ce" },
        ink: { DEFAULT: "#0f172a", muted: "#475569", faint: "#64748b" },
        ok: { DEFAULT: "#15803d", soft: "#ecfdf3", line: "#86efac", bright: "#22c55e" },
        warn: { DEFAULT: "#b45309", soft: "#fffbeb", line: "#fcd34d", bright: "#f59e0b" },
        crit: { DEFAULT: "#b91c1c", soft: "#fef2f2", line: "#fca5a5", bright: "#dc2626" },
        accent: { DEFAULT: "#1d4ed8", soft: "#eff6ff", line: "#93c5fd" },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(15, 23, 42, 0.06)",
      },
      keyframes: {
        pulseDot: { "0%,100%": { opacity: "1" }, "50%": { opacity: "0.35" } },
      },
      animation: { "pulse-dot": "pulseDot 1.6s ease-in-out infinite" },
    },
  },
  plugins: [],
};
