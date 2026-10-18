"use client";

import { useEffect, useId, useState } from "react";

/** The product's colours for mermaid, read from the CSS variables at render time (mermaid needs real colours; no literals here). */
function themeVariables(): Record<string, string> {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    primaryColor: v("--plane-raised"), primaryBorderColor: v("--edge"), primaryTextColor: v("--fg"),
    secondaryColor: v("--plane-sidebar"), tertiaryColor: v("--plane-pane"), lineColor: v("--fg-soft"),
    edgeLabelBackground: v("--plane-raised"), fontSize: "13px",
  };
}

/** A ```mermaid fence drawn inline. `securityLevel: "strict"` sanitises labels; the SVG is mermaid's own output. */
export function MermaidBlock({ source }: { source: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [state, setState] = useState<{ svg: string } | { error: string } | null>(null);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        // mermaid's config is global and LobsterGraphView initialises it too, so set ours (with the
        // theme as it is now, after any light/dark toggle) before every render. suppressErrorRendering
        // stops a failed render leaving mermaid's error SVG in document.body.
        // The font must be the one the labels render in: mermaid measures label widths with the font it is
        // told about, and "inherit" measured a fallback face, so Source Sans labels were clipped on device.
        const fontFamily = getComputedStyle(document.body).fontFamily || "inherit";
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true, theme: "base", fontFamily, themeVariables: { ...themeVariables(), fontFamily } });
        const { svg } = await mermaid.render(`m${id}`, source);
        if (live) setState({ svg });
      } catch (e) {
        if (live) setState({ error: (e as Error).message });
      }
    })();
    return () => { live = false; };
  }, [id, source]);
  if (!state) return <pre className="rounded-sm p-2 text-caption font-mono" style={{ background: "var(--edge)" }}>{source}</pre>;
  if ("error" in state) return <>
    <p className="text-caption text-muted-foreground">Couldn&apos;t draw this diagram.</p>
    <pre className="rounded-sm p-2 text-caption font-mono overflow-x-auto" style={{ background: "var(--edge)" }}>{source}</pre>
  </>;
  // mermaid's sanitised SVG (securityLevel strict), rendered once per source
  return <div role="img" aria-label="Diagram" className="openui-diagram overflow-x-auto my-2" dangerouslySetInnerHTML={{ __html: state.svg }} />;
}
