import type { ComponentType } from "react";
import type { CardName } from "@lightbulb/shared";
import type { RenderProps } from "./shared";

export type { RenderProps, SubComponent } from "./shared";

function TextView({ props }: RenderProps<{ text: string; size?: "body" | "small" | "heading" }>) {
  const cls = props.size === "heading" ? "text-base font-semibold" : props.size === "small" ? "text-caption text-muted-foreground" : "text-ui";
  return <p className={cls}>{props.text}</p>;
}

function StackView({ props, renderNode }: RenderProps<{ children: unknown }>) {
  return <div className="openui-stack">{renderNode(props.children)}</div>;
}

// Tasks 6 and 7 add the rest; until then an unbound name renders nothing (the parser
// still validates it, so the agent's prompt is unaffected).
export const RENDERERS: Partial<Record<CardName, ComponentType<RenderProps<never>>>> = {
  Stack: StackView as ComponentType<RenderProps<never>>,
  Text: TextView as ComponentType<RenderProps<never>>,
};
