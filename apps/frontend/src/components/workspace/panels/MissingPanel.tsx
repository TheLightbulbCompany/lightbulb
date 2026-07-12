"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { X } from "lucide-react";

// Terminal state for a persisted panel whose component key no longer maps to a
// live panel (the deserializer defaults any unknown contentComponent to the
// literal `'unknown'`, which the components map routes here). Nothing to render
// but an explanation and a way out.
export function MissingPanel(props: IDockviewPanelProps): React.ReactElement {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-sm text-[#8a8578]">
      <p>This panel is no longer available.</p>
      <button
        className="rounded border border-[#e0dbd0] px-3 py-1 hover:bg-[#f3efe6]"
        onClick={() => props.api.close()}
      >
        <X size={14} className="mr-1 inline" /> Close panel
      </button>
    </div>
  );
}
