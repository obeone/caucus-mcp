/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Caucus design-system palette — matches the legacy CSS custom properties
        bg: "#0a0e12",
        panel: "#11171d",
        "panel-2": "#161e26",
        line: "#233039",
        ink: "#c9d6de",
        dim: "#6b7d89",
        amber: "#ffb22e",
        green: "#4fd67a",
        red: "#ff4d5e",
        cyan: "#38c6d9",
        human: "#c08bff",
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      fontFamily: {
        mono: ['"IBM Plex Mono"', '"JetBrains Mono"', "monospace"],
        chrome: ['"Chakra Petch"', "sans-serif"],
        body: ['"iA Writer Quattro S"', "Georgia", "serif"],
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      animation: {
        "slide-in": "slideIn 0.22s cubic-bezier(.2,.7,.4,1)",
        blink: "blink 1.1s steps(2) infinite",
        "fade-in": "fadeIn 0.16s ease",
        "wizard-in":
          "wizardIn 0.2s cubic-bezier(.2,.7,.4,1)",
      },
      keyframes: {
        slideIn: {
          from: { opacity: "0", transform: "translateX(-10px)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
        blink: {
          "50%": { opacity: "0.35" },
        },
        fadeIn: {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        wizardIn: {
          from: { opacity: "0", transform: "translateY(-18px) scale(0.98)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
    },
  },
  plugins: [],
};
