"use client";

import { createLibrary, defineComponent } from "@openuidev/react-lang";
import { CARD_ROOT, type CardName } from "@lightbulb/shared";
import { CARD_COMPONENTS } from "@lightbulb/shared/cards";
import { RENDERERS } from "./renderers";

const Empty = () => null;

/** The shared vocabulary bound to this app's renderers. Props come from shared, never redeclared. */
export const webCardLibrary = createLibrary({
  root: CARD_ROOT,
  components: CARD_COMPONENTS.map((c) =>
    defineComponent({
      name: c.name,
      description: c.description,
      props: c.props,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- OpenUI types the renderer by each schema; the map is checked by name in renderers/index.tsx
      component: (RENDERERS[c.name as CardName] ?? Empty) as any,
    }),
  ),
});
