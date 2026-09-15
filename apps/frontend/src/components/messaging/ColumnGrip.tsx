"use client";

/** A visible grab bar on a column's left edge. Left arrow widens the column (it grows leftward). */
export function ColumnGrip({ onDrag, onNudge, onCommit, value, min, max }: {
  onDrag: (clientX: number) => void; onNudge: (deltaPx: number) => void; onCommit: () => void;
  value: number; min: number; max: number;
}) {
  return <div role="separator" aria-orientation="vertical" aria-label="Resize session column" tabIndex={0} title="Drag to resize" className="room-column-grip"
    aria-valuenow={Math.round(value)}
    aria-valuemin={Math.round(min)}
    aria-valuemax={Math.round(max)}
    onPointerDown={(event) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
    }}
    onPointerMove={(event) => { if (event.buttons & 1) onDrag(event.clientX); }}
    onPointerUp={(event) => {
      // Release only -- the commit happens on lostpointercapture below.
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    }}
    // No separate onPointerCancel: per the Pointer Events spec, cancelling a
    // captured pointer implicitly releases capture right after pointercancel
    // fires, which fires lostpointercapture in turn -- so pointercancel
    // always cascades into this handler too. Committing on both would double
    // commit a cancelled gesture in a real browser (jsdom does not cascade
    // the event, which is why a naive test would not catch it).
    onLostPointerCapture={() => onCommit()}
    onKeyDown={(event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      onNudge(event.key === "ArrowLeft" ? 24 : -24);
      onCommit();
    }} />;
}
