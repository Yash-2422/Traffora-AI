/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#070b14",
          900: "#0b1220",
          850: "#0f172a",
          800: "#131c31",
          700: "#1c2740",
          600: "#2a3855",
        },
        accent: {
          DEFAULT: "#22d3ee",
          soft: "#67e8f9",
          deep: "#0891b2",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 20px 40px -24px rgba(0,0,0,0.9)",
      },
      keyframes: {
        pulseRing: {
          "0%": { transform: "scale(0.8)", opacity: "0.9" },
          "100%": { transform: "scale(2.2)", opacity: "0" },
        },
      },
      animation: {
        pulseRing: "pulseRing 1.8s ease-out infinite",
      },
    },
  },
  plugins: [],
};
