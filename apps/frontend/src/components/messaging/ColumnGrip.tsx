"use client";

/** A visible grab bar on a column's left edge. Left arrow widens the column (it grows leftward). */
export function ColumnGrip({ onDrag, onNudge, onCommit, label = "Resize session column", value, min, max }: {
  onDrag: (clientX: number) => void; onNudge: (deltaPx: number) => void; onCommit: () => void; label?: string;
  value?: number; min?: number; max?: number;
}) {
  return <div role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0} title="Drag to resize" className="room-column-grip"
    aria-valuenow={value === undefined ? undefined : Math.round(value)}
    aria-valuemin={min === undefined ? undefined : Math.round(min)}
    aria-valuemax={max === undefined ? undefined : Math.round(max)}
    onPointerDown={(event) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
    }}
    onPointerMove={(event) => { if (event.buttons & 1) onDrag(event.clientX); }}
    onPointerUp={(event) => {
      // Release only -- the commit happens on lostpointercapture below, which
      // fires right after a release AND on cancellation/loss without one, so
      // there is exactly one commit per gesture regardless of how it ends.
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    }}
    onPointerCancel={() => onCommit()}
    onLostPointerCapture={() => onCommit()}
    onKeyDown={(event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      onNudge(event.key === "ArrowLeft" ? 24 : -24);
      onCommit();
    }} />;
}
