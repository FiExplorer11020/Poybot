import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Space Grotesk", "Inter", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"]
      },
      borderRadius: {
        '3xl': '1.5rem'
      },
      colors: {
        neon: "#00ff9d"
      },
      boxShadow: {
        neon: "0 0 18px rgba(0,255,157,0.25)"
      }
    }
  },
  plugins: []
};

export default config;
