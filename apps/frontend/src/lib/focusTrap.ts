const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Keeps Tab inside `containers` (in order): from the last stop back to the first, and Shift+Tab the other way.
 *  Call from the modal's keydown. */
export function trapTab(event: Pick<KeyboardEvent, "key" | "shiftKey" | "preventDefault">, containers: readonly (Element | null | undefined)[]): void {
  if (event.key !== "Tab") return;
  const stops = containers.flatMap((c) => c ? [...c.querySelectorAll<HTMLElement>(FOCUSABLE)] : [])
    .filter((el) => !el.closest("[hidden], [inert]"));
  if (!stops.length) return;
  const active = document.activeElement as HTMLElement | null;
  const at = active ? stops.indexOf(active) : -1;
  if (event.shiftKey ? at <= 0 : at === -1 || at === stops.length - 1) {
    event.preventDefault();
    (event.shiftKey ? stops[stops.length - 1] : stops[0]).focus();
  }
}
