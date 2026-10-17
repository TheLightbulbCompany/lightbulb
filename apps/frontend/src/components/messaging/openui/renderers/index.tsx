import type { ComponentType } from "react";
import type { CardName } from "@lightbulb/shared";
import { BarChartView, LineChartView } from "./charts";
import { CalloutView, ColView, EditableTableView, ImageView, MetricView, SourcesView, StepsView, TableView } from "./show";
import { ButtonView, ButtonsView, ChoiceView, DatePickerView, FollowUpsView, FormView, InputView, SelectView } from "./ask";
import type { RenderProps } from "./shared";

export type { RenderProps, SubComponent } from "./shared";

function TextView({ props }: RenderProps<{ text: string; size?: "body" | "small" | "heading" }>) {
  const cls = props.size === "heading" ? "text-base font-semibold" : props.size === "small" ? "text-caption text-muted-foreground" : "text-ui";
  return <p className={cls}>{props.text}</p>;
}

function StackView({ props, renderNode }: RenderProps<{ children: unknown }>) {
  return <div className="openui-stack">{renderNode(props.children)}</div>;
}

export const RENDERERS: Record<CardName, ComponentType<RenderProps<never>>> = {
  Stack: StackView as ComponentType<RenderProps<never>>,
  Text: TextView as ComponentType<RenderProps<never>>,
  Callout: CalloutView as ComponentType<RenderProps<never>>,
  Metric: MetricView as ComponentType<RenderProps<never>>,
  Table: TableView as ComponentType<RenderProps<never>>,
  Col: ColView as ComponentType<RenderProps<never>>,
  EditableTable: EditableTableView as ComponentType<RenderProps<never>>,
  Steps: StepsView as ComponentType<RenderProps<never>>,
  Sources: SourcesView as ComponentType<RenderProps<never>>,
  Image: ImageView as ComponentType<RenderProps<never>>,
  BarChart: BarChartView as ComponentType<RenderProps<never>>,
  LineChart: LineChartView as ComponentType<RenderProps<never>>,
  Choice: ChoiceView as ComponentType<RenderProps<never>>,
  Buttons: ButtonsView as ComponentType<RenderProps<never>>,
  Button: ButtonView as ComponentType<RenderProps<never>>,
  FollowUps: FollowUpsView as ComponentType<RenderProps<never>>,
  Form: FormView as ComponentType<RenderProps<never>>,
  Input: InputView as ComponentType<RenderProps<never>>,
  Select: SelectView as ComponentType<RenderProps<never>>,
  DatePicker: DatePickerView as ComponentType<RenderProps<never>>,
};
