// Dock groups must never be crushable to the point their content cannot render.
//
// dockview ships NO default minimum for a group, and layoutStorage persists
// group sizes verbatim — so one sash drag past the edge leaves a group at
// roughly its 35px header with all of its content unreachable, and every
// reload restores it that way. Measured in the wild: a Dashboard slot 63px
// tall holding 2046px of content, which reads as "the page is broken" rather
// than "the pane is collapsed".
//
// Two halves, because they fix different things:
//
//   setConstraints — records the bound. dockview's gridviewPanel applies it
//   to the panel's _minimumWidth/_minimumHeight WITHOUT triggering a
//   relayout, so this prevents FUTURE squeezes (splitview honours minimums
//   while dragging) but cannot repair a layout that already restored below
//   the bound.
//
//   setSize — the repair. A group that came back from localStorage too small
//   is pushed back to the minimum, and dockview redistributes from its
//   siblings. Without this, an already-squeezed layout stays squeezed until
//   the user happens to drag the sash themselves.
//
// The tab bar is 35px, so the height floor leaves ~85px of content: enough to
// see the panel is alive and to grab the sash. Deliberately not larger —
// several stacked groups in a short dock would then all fight their minimums.
export const MIN_GROUP_HEIGHT = 120;

// Width has the same failure mode and never had a bound. Panels in this dock
// render real pages (a Tasks board, a masthead + toolbar, a chat composer),
// and below roughly 300px those collapse into unreadable slivers rather than
// degrading. 300 is the narrowest width at which the panel chrome — tab strip,
// masthead, and a single content column — still lays out.
export const MIN_GROUP_WIDTH = 300;

/** The slice of dockview's group API this needs — narrow so the logic is
 * testable without standing up a real dock. */
export interface ConstrainableGroup {
  api: {
    readonly height: number;
    readonly width: number;
    setConstraints(value: { minimumHeight?: number; minimumWidth?: number }): void;
    setSize(value: { height?: number; width?: number }): void;
  };
}

/**
 * Floor a group on both axes, and repair it if it restored below either bound.
 *
 * The repair is issued as a single setSize call carrying only the axes that
 * are actually short. dockview redistributes from siblings per axis, so
 * sending an already-healthy axis would hand it a size it never asked for.
 */
export function constrainGroup(
  group: ConstrainableGroup,
  minimumHeight: number = MIN_GROUP_HEIGHT,
  minimumWidth: number = MIN_GROUP_WIDTH,
): void {
  group.api.setConstraints({ minimumHeight, minimumWidth });

  const repair: { height?: number; width?: number } = {};
  if (group.api.height < minimumHeight) repair.height = minimumHeight;
  if (group.api.width < minimumWidth) repair.width = minimumWidth;
  if (repair.height !== undefined || repair.width !== undefined) {
    group.api.setSize(repair);
  }
}

/**
 * @deprecated Use {@link constrainGroup}. Kept so existing call sites and the
 * height-only contract tests keep compiling; it now also applies the width
 * bound, which is the point of the change.
 */
export function constrainGroupHeight(
  group: ConstrainableGroup,
  minimumHeight: number = MIN_GROUP_HEIGHT,
): void {
  constrainGroup(group, minimumHeight);
}
