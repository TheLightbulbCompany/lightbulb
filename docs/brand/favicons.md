# Favicons — the adaptive pack

From the designer's favicon pack (trimmed from its USAGE.md). Two marks:
`product` (Lightbulb app) and `company` (thelightbulb.company).

- The adaptive SVGs carry both colourways and switch on
  `prefers-color-scheme`. Modern browsers use the SVG and ignore the PNGs;
  the sized PNGs (32/16) are the fallback for browsers that don't.
- The shipped PNGs are the **light** colourway (midnight/brass, for light tab
  strips) with transparent backgrounds.
- The apple-touch icons are the only **opaque** files: iOS composites
  transparency onto black, so they are flattened onto the brand cream
  (`--cream`, `#F5EFE2`) at 180×180.
- `.ico` is legacy fallback only (contains 32 + 16).

## Where they live

**Product app** (`apps/frontend/src/app/`) — Next.js metadata file
conventions, no manual `<link>`s:
`icon.svg` (adaptive), `icon1.png` (32), `icon2.png` (16), `icon.png` (512),
`apple-icon.png` (opaque 180), `favicon.ico`.

**Company site** (`apps/company/public/`) — declared via `metadata.icons` in
`src/app/layout.tsx`:
`favicon.svg` (adaptive), `favicon-32.png`, `favicon-16.png`, `icon-512.png`,
`apple-touch-icon.png` (opaque 180).

## Regenerating

From a light 512 source: `sips -z 32 32` / `-z 16 16` / `-z 180 180`, flatten
the 180 onto `#F5EFE2` (PIL `alpha_composite`), and
`magick 32.png 16.png favicon.ico`.
