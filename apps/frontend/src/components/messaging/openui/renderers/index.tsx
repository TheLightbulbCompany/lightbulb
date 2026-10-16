import type { ComponentType } from "react";
import type { CardName } from "@lightbulb/shared";
import { Button as UiButton } from "@/components/ui/button";
import { useCardPress, type RenderProps, type SubComponent } from "./shared";

export type { RenderProps, SubComponent } from "./shared";

function TextView({ props }: RenderProps<{ text: string; size?: "body" | "small" | "heading" }>) {
  const cls = props.size === "heading" ? "text-base font-semibold" : props.size === "small" ? "text-caption text-muted-foreground" : "text-ui";
  return <p className={cls}>{props.text}</p>;
}

function StackView({ props, renderNode }: RenderProps<{ children: unknown }>) {
  return <div className="openui-stack">{renderNode(props.children)}</div>;
}

function ButtonsView({ props }: RenderProps<{ buttons: SubComponent<{ label: string; reply?: string; variant?: "primary" | "secondary" }>[] }>) {
  const { press, disabled, answered } = useCardPress();
  return (
    <div className="flex flex-wrap gap-2 openui-live" role="group">
      {props.buttons.map((b, i) => {
        const text = (b.props.reply ?? b.props.label).trim();
        const picked = answered !== null && answered === text;
        return <UiButton key={`${b.props.label}-${i}`} type="button" size="sm" variant={b.props.variant === "secondary" ? "outline" : "default"}
          disabled={disabled} aria-pressed={picked} onClick={() => press(text)}>{b.props.label}</UiButton>;
      })}
    </div>
  );
}

// Tasks 6 and 7 add the rest; until then an unbound name renders nothing (the parser
// still validates it, so the agent's prompt is unaffected).
export const RENDERERS: Partial<Record<CardName, ComponentType<RenderProps<never>>>> = {
  Stack: StackView as ComponentType<RenderProps<never>>,
  Buttons: ButtonsView as ComponentType<RenderProps<never>>,
  Text: TextView as ComponentType<RenderProps<never>>,
};
