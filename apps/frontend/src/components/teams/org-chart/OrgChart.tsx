"use client";

// apps/frontend/src/components/teams/org-chart/OrgChart.tsx

// Ported from upstream Paperclip's OrgChart render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Renders positioned AgentCards + SVG edges. No pan/zoom for v1.

import { useMemo } from "react";
import { AgentCard } from "./AgentCard";
import {
  CARD_W,
  CARD_H,
  flattenEdges,
  flattenTree,
  layoutTree,
  type OrgChartAgent,
} from "./orgChartLayout";

export interface OrgChartProps {
  agents: OrgChartAgent[];
  className?: string;
}

export function OrgChart({ agents, className }: OrgChartProps) {
  const layout = useMemo(() => layoutTree(agents), [agents]);

  if (agents.length === 0) {
    return (
      <div className={className}>
        <div className="flex flex-col items-center justify-center py-12 text-sm text-muted-foreground">
          No agents yet.
        </div>
      </div>
    );
  }

  const cards = flattenTree(layout.roots);
  const edges = flattenEdges(layout.roots);

  return (
    <div className={className}>
      <div className="overflow-auto">
        <div
          className="relative"
          style={{ width: layout.width, height: layout.height, minWidth: "100%" }}
          data-testid="org-chart-canvas"
        >
          <svg
            className="absolute inset-0 pointer-events-none"
            width={layout.width}
            height={layout.height}
            aria-hidden="true"
          >
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
                  stroke="currentColor"
                  strokeWidth={1.5}
                  className="text-border"
                  data-org-edge={`${from.id}->${to.id}`}
                />
              );
            })}
          </svg>

          {cards.map((c) => (
            <AgentCard
              key={c.id}
              id={c.id}
              name={c.name}
              role={c.role}
              status={c.status}
              x={c.x}
              y={c.y}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
