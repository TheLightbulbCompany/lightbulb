import type { ReactNode } from "react";

/** Props every renderer receives from OpenUI: the parsed props plus `renderNode` for children. */
export interface RenderProps<P = Record<string, unknown>> {
  props: P;
  renderNode: (value: unknown) => ReactNode;
}

/** A parsed child component as the parent sees it. */
export interface SubComponent<P = Record<string, unknown>> {
  type: "element";
  typeName: string;
  props: P;
  partial: boolean;
}
