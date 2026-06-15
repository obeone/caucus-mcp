# Caucus Operator Dashboard — web/

React + TypeScript + Tailwind + shadcn/ui frontend for the Caucus hub.

## Development

```bash
cd web/
npm install
npm run dev       # Vite dev server at http://localhost:5173
```

The dev server proxies `/ui` to the hub if you add a proxy in `vite.config.ts`
(not wired by default — run the hub separately and open the dashboard from
`http://127.0.0.1:8765/` which serves the built bundle).

## Production build

```bash
npm run build
```

Emits the bundle into `../src/caucus/ui/` so the hub can serve it directly
from Python package data at `/`. The built bundle is committed to the repo.
Source maps are gitignored.

## Auth

If the hub is started with `--operator-token`, pass the token as:
- URL query param: `?token=<token>`
- Or stored in `localStorage["caucus_token"]`

Without a token configured the dashboard connects as operator without auth.

## Stack

- Vite 6 + React 18 + TypeScript strict
- Tailwind CSS 3 (dark mode via `class`)
- Radix UI primitives (Dialog, Toast, etc.)
- Zustand store + native WebSocket client
- @tanstack/react-virtual for the 500-message timeline
- Lucide React icons
