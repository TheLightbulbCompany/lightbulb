import { useState, type ReactNode } from "react";
import { useTriggerAction } from "@openuidev/react-lang";
import { useCardContext, useCardLive } from "../CardContext";

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

/** A press posts once: the button locks while its reply is in flight or once the card is answered. */
export function useCardPress() {
  const ctx = useCardContext();
  const live = useCardLive();
  const trigger = useTriggerAction();
  const [busy, setBusy] = useState(false);
  const press = (text: string) => {
    if (!live || busy) return;
    setBusy(true);
    trigger(text);
  };
  return { press, disabled: !live || busy, answered: ctx?.answered ?? null };
}
