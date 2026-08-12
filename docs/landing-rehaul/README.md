# Landing page rehaul — static preview

Standalone HTML previews for the landing-page rehaul. **Nothing here is wired
into Next.js.** Open `landing.html` in a browser to review; the four nav/link
destinations are real pages in this folder so the flow is clickable.

```
landing.html      the page under review
how-it-works.html
pricing.html
blog.html
faq.html
_page.css         shared shell for the four sub-pages
```

## What changed versus the current landing page

The live page (`apps/frontend/src/app/page.tsx`) composes eleven sections. This
is five: hero, three product blocks, a link index, footer. Dropped: the Features
card grid, the Isolation block, the Teams-inbox mock, the `ld-rule` label bands,
and GooseTown.

Design direction was drawn from hf0.com, sierra.ai, cognition.com, illoca.com,
incredibles.dev and datum.xyz — architecture and motion patterns, with isol8
copy and tokens throughout.

## Two findings that need a decision before this ships

### 1. The wordmark does not currently render in Georgia

`globals.css:1455` and every landing heading declare:

```css
font-family: var(--font-lora-serif), Georgia, serif;
```

Lora loads, so **Georgia is a fallback that never fires**. If the standalone `8`
mark is Georgia, nothing on the page matches it today. Measuring the real vector
in `isol8-8-cutout/` against installed faces: Georgia Bold matches its
proportion almost exactly (0.788 vs 0.779 width/height) while a geometric sans
matches the drawn shape better (0.88 vs 0.79 overlap). Not conclusive — worth
confirming against whatever file the mark was originally set in.

These previews use real Georgia, with per-pair optical tracking. Georgia's
default uniform tracking leaves the `L→8` pair ~24% looser than the others,
because `L` has a zero right sidebearing while `8` carries 41 units of its own.
Equalising the perceived gaps gives:

| pair | tracking |
|------|----------|
| I→S  | `.125em` |
| S→O  | `.149em` |
| O→L  | `.106em` |
| L→8  | `.070em` |

Georgia's `8` is an ascending old-style figure that lands within 1px of cap `O`
(730 vs 731, same overshoot), which is why the wordmark sits evenly at all.

### 2. The `$70` price is a checkout total, not a monthly rate

`pricing.html` states the Claude plan as "$50 per month plus $20 of Claude
credits, charged together at checkout" rather than `$70/mo`, because that is
what `create_flat_fee_checkout` actually charges.

Per the comment at `lib/providerCards.ts:94-113`, that string **is the
pre-payment disclosure** and must stay in sync with
`BEDROCK_STARTER_CREDITS_CENTS`. Live copy currently says "from $5" deliberately,
since a new signup gets a card-less trial rather than a charge. Showing a flat
`$70` is a product decision, not a copy tweak.

## Notes for whoever ports this

- Libraries: GSAP + ScrollTrigger (gsap is already a dependency) and Lenis for
  smooth scroll, all from CDN in the preview. ScrollTrigger + Lenis are new.
- The roster constellation reuses the node coordinates and edge list from
  `components/landing/Team.tsx` unchanged. Note it renders with
  `preserveAspectRatio="none"`, which stretches the coordinate space to the
  container, so the figure is not uniform at every viewport width.
- Marketplace rows are hard-coded here. The live component reads real listings
  via `featuredListings()` — keep that.
- `prefers-reduced-motion` is honoured throughout.
- The three product blocks use diagrams where real product screenshots would
  land harder.
