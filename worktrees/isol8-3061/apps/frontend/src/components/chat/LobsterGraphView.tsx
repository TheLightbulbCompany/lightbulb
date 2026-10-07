"use client";

import * as React from "react";
import { ZoomIn, ZoomOut, RotateCcw, Copy, FileText, Loader2 } from "lucide-react";
import { lobsterToMermaid } from "@/lib/lobster";
import { LOBSTER_MERMAID_THEME } from "./lobsterMermaidBrand";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface LobsterGraphViewProps {
  content: string;
  onViewCode?: () => void;
}

// Themed once, shared across instances. theme:"base" + a full themeVariables
// set keeps the diagram inside the product's cream/midnight palette instead of
// mermaid's default blues. securityLevel:"strict" sanitizes labels since we
// inject the SVG via dangerouslySetInnerHTML.
const MERMAID_CONFIG = {
  startOnLoad: false,
  securityLevel: "strict",
  theme: "base",
  fontFamily: "ui-sans-serif, system-ui, sans-serif",
  themeVariables: {
    ...LOBSTER_MERMAID_THEME,
    fontSize: "13px",
  },
} as const;

let mermaidInitialized = false;

function Fallback({ message, onViewCode }: { message: string; onViewCode?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-sm text-[var(--midnight-muted)]">
      <p>{message}</p>
      {onViewCode ? (
        <button
          onClick={onViewCode}
          className="flex items-center gap-1 px-2 py-1 text-xs rounded-sm border border-[var(--edge)] hover:bg-[var(--cream-deep)] transition-colors"
        >
          <FileText className="h-3 w-3" /> View as code
        </button>
      ) : null}
    </div>
  );
}

// Renders a .lobster workflow as a Mermaid flowchart. Mermaid is imported
// dynamically so it stays out of the main chat bundle until a graph is shown.
export function LobsterGraphView({ content, onViewCode }: LobsterGraphViewProps) {
  const result = React.useMemo(() => lobsterToMermaid(content), [content]);
  const [svg, setSvg] = React.useState<string | null>(null);
  const [renderError, setRenderError] = React.useState<string | null>(null);
  const [scale, setScale] = React.useState(1);

  const reactId = React.useId();
  const diagramId = React.useMemo(() => "lobster-" + reactId.replace(/[^a-zA-Z0-9]/g, ""), [reactId]);

  React.useEffect(() => {
    if (!result.ok) return;
    let cancelled = false;
    setSvg(null);
    setRenderError(null);
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!mermaidInitialized) {
          mermaid.initialize(MERMAID_CONFIG);
          mermaidInitialized = true;
        }
        const { svg } = await mermaid.render(diagramId, result.mermaid);
        if (!cancelled) setSvg(svg);
      } catch (err) {
        if (!cancelled) setRenderError((err as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [result, diagramId]);

  if (!result.ok) {
    const message =
      result.reason === "parse" ? "Couldn't render this workflow as a graph." : result.message;
    return <Fallback message={message} onViewCode={onViewCode} />;
  }

  const iconBtn =
    "p-1 rounded-sm text-[var(--midnight-muted)] hover:bg-[var(--edge)] transition-colors disabled:opacity-50";

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-1 px-3 py-1.5 bg-[var(--cream-deep)] border-b border-[var(--edge)] shrink-0">
        <Tooltip>
          <TooltipTrigger asChild>
            <button className={iconBtn} aria-label="Zoom in" onClick={() => setScale((s) => Math.min(s + 0.2, 3))}>
              <ZoomIn className="h-4 w-4" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Zoom in</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <button className={iconBtn} aria-label="Zoom out" onClick={() => setScale((s) => Math.max(s - 0.2, 0.4))}>
              <ZoomOut className="h-4 w-4" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Zoom out</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <button className={iconBtn} aria-label="Reset zoom" onClick={() => setScale(1)}>
              <RotateCcw className="h-4 w-4" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Reset zoom</TooltipContent>
        </Tooltip>
        <span className="text-2xs text-[var(--midnight-muted)] ml-1">{Math.round(scale * 100)}%</span>
        <button
          className="ml-auto flex items-center gap-1 px-2 py-1 text-xs rounded-sm text-[var(--midnight-muted)] hover:bg-[var(--edge)] transition-colors"
          aria-label="Copy mermaid source"
          onClick={() => navigator.clipboard?.writeText(result.mermaid)}
        >
          <Copy className="h-3 w-3" /> Copy source
        </button>
      </div>
      <div className="flex-1 overflow-auto p-4 bg-cream">
        {renderError ? (
          <div className="text-sm">
            <p className="text-err mb-2">Couldn&apos;t render the diagram.</p>
            <pre className="text-xs bg-[var(--cream-deep)] p-3 rounded-sm overflow-auto whitespace-pre-wrap">
              {result.mermaid}
            </pre>
          </div>
        ) : svg === null ? (
          <div className="flex items-center justify-center h-full text-[var(--midnight-muted)] text-sm">
            <Loader2 className="h-5 w-5 animate-spin mr-2" /> Rendering graph…
          </div>
        ) : (
          <div
            style={{ transform: `scale(${scale})`, transformOrigin: "top left" }}
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        )}
      </div>
    </div>
  );
}
