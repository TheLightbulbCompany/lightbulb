"use client";

/** A visible grab bar on a column's left edge. Left arrow widens the column (it grows leftward). */
export function ColumnGrip({ onDrag, onNudge, onCommit, label = "Resize session column" }: {
  onDrag: (clientX: number) => void; onNudge: (deltaPx: number) => void; onCommit: () => void; label?: string;
}) {
  return <div role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0} title="Drag to resize" className="room-column-grip"
    onPointerDown={(event) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
    }}
    onPointerMove={(event) => { if (event.buttons & 1) onDrag(event.clientX); }}
    onPointerUp={(event) => { event.currentTarget.releasePointerCapture(event.pointerId); onCommit(); }}
    onKeyDown={(event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      onNudge(event.key === "ArrowLeft" ? 24 : -24);
      onCommit();
    }} />;
}
