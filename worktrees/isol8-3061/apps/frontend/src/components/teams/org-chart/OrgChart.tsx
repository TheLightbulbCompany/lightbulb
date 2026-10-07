"use client";

// apps/frontend/src/components/teams/org-chart/OrgChart.tsx

// Ported from upstream Paperclip's OrgChart render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Renders positioned AgentCards + SVG edges inside a pan/zoom viewport:
// drag to pan, wheel/buttons to zoom (clamped), and a fit-to-screen reset.
// Both the SVG edge layer and the card layer share the same
// `translate(pan) scale(zoom)` transform so edges stay glued to cards. The
// pan/zoom state machine lives in `useOrgPanZoom`.

import { useEffect, useMemo, useRef } from "react";
import { Maximize2, Minus, Plus } from "lucide-react";
import { AgentCard } from "./AgentCard";
import { useOrgPanZoom } from "./useOrgPanZoom";
import {
  CARD_W,
  CARD_H,
  flattenEdges,
  flattenTree,
  layoutTree,
  type OrgChartAgent,
} from "./orgChartLayout";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export interface OrgChartProps {
  agents: OrgChartAgent[];
  className?: string;
}

export function OrgChart({ agents, className }: OrgChartProps) {
  const layout = useMemo(() => layoutTree(agents), [agents]);

  const containerRef = useRef<HTMLDivElement>(null);
  const bounds = useMemo(
    () => ({ width: layout.width, height: layout.height }),
    [layout.width, layout.height],
  );
  const { pan, zoom, dragging, onMouseDown, onMouseMove, onMouseUp, zoomIn, zoomOut, fitToScreen } =
    useOrgPanZoom(containerRef, bounds);

  // Fit the chart to the viewport the first time it has real bounds. Keyed on
  // the layout dimensions so a freshly-loaded (or restructured) chart recenters
  // once rather than fighting the user's manual pan on every re-render.
  const fittedKey = useRef<string | null>(null);
  useEffect(() => {
    if (layout.width <= 0 || layout.height <= 0) return;
    const key = `${layout.width}x${layout.height}`;
    if (fittedKey.current === key) return;
    fittedKey.current = key;
    fitToScreen();
  }, [layout.width, layout.height, fitToScreen]);

  if (agents.length === 0) {
    return (
      <div className={className}>
        <div
          className="flex flex-col items-center justify-center py-12 text-sm"
          style={{ color: "var(--midnight-muted)" }}
        >
          No agents yet.
        </div>
      </div>
    );
  }

  const cards = flattenTree(layout.roots);
  const edges = flattenEdges(layout.roots);
  const transform = `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`;

  return (
    <div className={className}>
      <div
        ref={containerRef}
        data-testid="org-chart-viewport"
        role="application"
        aria-label="Org chart (drag to pan, scroll to zoom)"
        // org-chart-viewport: inside a dock panel, workspace.css overrides
        // the viewport-height fallback below with a slot-relative height so
        // the canvas fills the panel. The class is inert in the mobile shell.
        className="org-chart-viewport editorial-card relative h-[calc(100dvh-16rem)] min-h-[420px] w-full overflow-hidden"
        style={{
          cursor: dragging ? "grabbing" : "grab",
          touchAction: "none",
          overscrollBehavior: "contain",
        }}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
      >
        {/* Zoom controls — editorial icon-button cluster, top-right. */}
        <div className="absolute right-3 top-3 z-10 flex flex-col gap-1.5">
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="editorial-btn editorial-btn--secondary editorial-btn--icon"
                onClick={zoomIn}
                aria-label="Zoom in"
              >
                <Plus size={16} aria-hidden="true" />
              </button>
            </TooltipTrigger>
            <TooltipContent>Zoom in</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="editorial-btn editorial-btn--secondary editorial-btn--icon"
                onClick={zoomOut}
                aria-label="Zoom out"
              >
                <Minus size={16} aria-hidden="true" />
              </button>
            </TooltipTrigger>
            <TooltipContent>Zoom out</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="editorial-btn editorial-btn--secondary editorial-btn--icon"
                onClick={fitToScreen}
                aria-label="Fit chart to screen"
              >
                <Maximize2 size={16} aria-hidden="true" />
              </button>
            </TooltipTrigger>
            <TooltipContent>Fit to screen</TooltipContent>
          </Tooltip>
        </div>

        {/* SVG edge layer — shares the viewport transform via the inner <g>. */}
        <svg
          className="absolute inset-0 pointer-events-none"
          width="100%"
          height="100%"
          aria-hidden="true"
        >
          <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
            {edges.map(({ from, to }) => {
              const fromX = from.x + CARD_W / 2;
              const fromY = from.y + CARD_H;
              const toX = to.x + CARD_W / 2;
              const toY = to.y;
              const midY = (fromY + toY) / 2;
              return (
                <path
                  key={`${from.id}-${to.id}`}
                  d={`M ${fromX} ${fromY} L ${fromX} ${midY} L ${toX} ${midY} L ${toX} ${toY}`}
                  fill="none"
                  stroke="var(--edge)"
                  strokeWidth={1.5}
                  data-org-edge={`${from.id}->${to.id}`}
                />
              );
            })}
          </g>
        </svg>

        {/* Card layer — same transform, origin top-left so it tracks the SVG. */}
        <div
          className="absolute inset-0"
          data-testid="org-chart-canvas"
          style={{ transform, transformOrigin: "0 0" }}
        >
          {cards.map((c) => (
            <AgentCard
              key={c.id}
              id={c.id}
              name={c.name}
              role={c.role}
              status={c.status}
              x={c.x}
              y={c.y}
              title={c.title}
              capabilities={c.capabilities}
              lastHeartbeatAt={c.lastHeartbeatAt}
              pauseReason={c.pauseReason}
              errorReason={c.errorReason}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
