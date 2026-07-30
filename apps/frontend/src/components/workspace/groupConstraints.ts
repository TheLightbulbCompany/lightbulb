// Dock groups must never be crushable down to their own tab bar.
//
// dockview ships NO default minimum height for a group, and layoutStorage
// persists group sizes verbatim — so one sash drag past the edge leaves a
// group at roughly its 35px header with all of its content unreachable, and
// every reload restores it that way. Measured in the wild: a Dashboard slot
// 63px tall holding 2046px of content, which reads as "the page is broken"
// rather than "the pane is collapsed".
//
// Two halves, because they fix different things:
//
//   setConstraints — records the bound. dockview's gridviewPanel applies it
//   to the panel's _minimumHeight WITHOUT triggering a relayout, so this
//   prevents FUTURE squeezes (splitview honours minimums while dragging)
//   but cannot repair a layout that already restored below the bound.
//
//   setSize — the repair. A group that came back from localStorage too short
//   is pushed back to the minimum, and dockview redistributes from its
//   siblings. Without this, an already-squeezed layout stays squeezed until
//   the user happens to drag the sash themselves.
//
// The tab bar is 35px, so this leaves ~85px of content: enough to see the
// panel is alive and to grab the sash. Deliberately not larger — several
// stacked groups in a short dock would then all fight their minimums.
export const MIN_GROUP_HEIGHT = 120;

/** The slice of dockview's group API this needs — narrow so the logic is
 * testable without standing up a real dock. */
export interface ConstrainableGroup {
  api: {
    readonly height: number;
    setConstraints(value: { minimumHeight?: number }): void;
    setSize(value: { height?: number }): void;
  };
}

export function constrainGroupHeight(
  group: ConstrainableGroup,
  minimumHeight: number = MIN_GROUP_HEIGHT,
): void {
  group.api.setConstraints({ minimumHeight });
  if (group.api.height < minimumHeight) {
    group.api.setSize({ height: minimumHeight });
  }
}
