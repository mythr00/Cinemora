# Frontend audit (2026-10-25)

This audit is based on the existing modified/untracked tree. No source was deleted or rewritten. `backend/docs/MASTER_PROGRESS.md` records the backup and initial dirty-tree state.

| Area | Purpose / evidence | Decision |
|---|---|---|
| `package.json` | Next 16.3.2, React 19.2.8, Tailwind 4; `dev`, `build`, `start`, `lint`; no Vitest/Playwright/axe/Zod scripts or dependencies. | KEEP; add phased test/client dependencies only when implementing F. |
| `app/page.tsx`, `globals.css`, `layout.tsx`, `public/*` | Marketing landing, global style tokens, metadata, marks. Existing user redesign is present. | KEEP; finish responsive/a11y audit without copying reference text/assets. |
| `/create` | Direct fetch to `/render-options/defaults`, `/health`, `/voices`, `/backgrounds`, `/music`, `/projects`, `/projects/{id}/voiceover`, `/projects/{id}/analyze`, `/transcribe-upload`, `/projects/{id}/thumbnail`; status polling every 3 seconds. | KEEP; typed API integration and feasibility remain. |
| `/projects` and `/studio` | Fetch `/projects`; list/status surfaces. | KEEP; unify status/error/polling behavior. |
| `/projects/[id]` | Fetch editor, candidates, render options, versions, status; POST refresh/assets/review/history/rerender; PUT editor. Status polling every 2.5 seconds. | KEEP; needs-footage, reports, graphics and richer editor contracts are absent. |
| `/channel-lab` | POST `/channel-research` and `/channel-research/script`; saves script draft to sessionStorage then links to create. | KEEP; preserve working integration. |
| `/viral-clips` | POST `/viral-clips` and `/viral-clips/upload`. | KEEP; preserve utility route. |
| Navigation/components | `components/marketing-navigation.tsx`, `components/app-navigation.tsx`, route layouts. | KEEP; build out shared design system in F. |
| API client/config | Pages call `fetch` directly; `NEXT_PUBLIC_API_URL` defaults to `http://127.0.0.1:8000`; no centralized client, Zod schemas, auth/session handling or SSE. | REFACTOR in F; define missing contracts first. |
| Polling/error handling | Create/editor use recursive `setTimeout`; polling has cleanup on route unmount, no visibility-aware pause or shared exponential backoff. | REFACTOR in F. |

Backend routes currently exposed to these pages include health, project CRUD/read/status/video, options, voices, music, backgrounds, channel research, editor/assets/review/history/candidates/versions, thumbnails, uploads and viral clips. Feasibility, needs-footage workflows, graphics plan editing, reports downloads, settings/profile, ownership/auth and resumable upload endpoints were not found in the route scan. Frontend should disable unavailable features and explain them; no production mock-data path exists today.

Responsive screenshots at 375, 768, 1280 and 1536 pixels and Lighthouse/axe measurements remain outstanding; frontend runtime access is being checked. No screenshot is claimed as captured.
