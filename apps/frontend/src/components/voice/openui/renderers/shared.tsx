import { createContext, useContext, type ReactNode } from "react";

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

export interface CardPress {
  press: (text: string) => void;
  disabled: boolean;
  answered: string | null;
  /** The reply of a send that got no answer: the next press replays it, and the other options wait. */
  retry: string | null;
}
/** Provided by OpenUICard from the message's press state (CardContext.useCardPressState). */
export const CardPressContext = createContext<CardPress>({ press: () => {}, disabled: true, answered: null, retry: null });
export const useCardPress = (): CardPress => useContext(CardPressContext);
