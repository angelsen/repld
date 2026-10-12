# repld site

Marketing pages, docs and blog for repld. Astro + Starlight + Tailwind, published to GitHub Pages under `/repld`.

## Layout

- `src/pages/`: editorial pages (home, quickstart, playbook, controls, production) and the blog routes.
- `src/content/docs/`: Starlight docs (guides and reference).
- `src/content/blog/`: blog posts, schema in `src/content.config.ts`.
- `src/scripts/reveal.ts`: shared Lenis + GSAP scroll-reveal; the home page adds its own boot preloader on top.

## Commands

Run from `site/`:

| Command          | Action                             |
| :--------------- | :--------------------------------- |
| `pnpm install`   | Install dependencies               |
| `pnpm dev`       | Dev server at `localhost:4321`     |
| `pnpm build`     | Build to `./dist/`                 |
| `pnpm preview`   | Serve the build locally            |
| `pnpm astro check` | Type-check pages and content     |

## Deploy

`make deploy` from the repo root builds the site and pushes it to the `gh-pages` branch.
