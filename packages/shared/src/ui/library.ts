/**
 * The card vocabulary an agent may write inside a ```openui-lang fence (spec A2).
 *
 * Framework-neutral: names, zod props and descriptions only, with `component: null`.
 * Web and the phone bind renderers to THESE objects (never redeclare props) so the
 * prompt the agent reads, the parser and both renderers agree.
 *
 * KEY ORDER IS THE WIRE FORMAT. OpenUI Lang passes arguments positionally in zod key
 * order, so reordering a key changes what every already-sent card means. Add new
 * optional keys at the END only.
 */
import { createLibrary, defineComponent } from "@openuidev/lang-core";
import { z } from "zod/v4";
import { CARD_ROOT } from "./constants";


export const Text = defineComponent({
  name: "Text", description: "A line or paragraph of prose.",
  props: z.object({ text: z.string(), size: z.enum(["body", "small", "heading"]).optional() }), component: null,
});
export const Callout = defineComponent({
  name: "Callout", description: "A short note that stands out: a caveat, a warning, a win.",
  props: z.object({ text: z.string(), tone: z.enum(["info", "warn", "good"]).optional() }), component: null,
});
export const Metric = defineComponent({
  name: "Metric", description: "One number with its label and an optional change, e.g. Metric(\"MRR\", \"$4,200\", \"+12%\").",
  props: z.object({ label: z.string(), value: z.string(), delta: z.string().optional() }), component: null,
});
export const Col = defineComponent({
  name: "Col", description: "One table column: its header and its cells, top to bottom.",
  props: z.object({ label: z.string(), data: z.array(z.union([z.string(), z.number()])) }), component: null,
});
export const Table = defineComponent({
  name: "Table", description: "A small table for a comparison; one Col per column, all the same length.",
  props: z.object({ columns: z.array(Col.ref) }), component: null,
});
export const EditableTable = defineComponent({
  name: "EditableTable", description: "A table the person edits in place; Submit posts the rows back as their reply.",
  props: z.object({ columns: z.array(z.string()), rows: z.array(z.array(z.string())), submit: z.string().optional() }), component: null,
});
export const BarChart = defineComponent({
  name: "BarChart", description: "A bar chart, one bar per label. Only when the shape of the numbers is the point.",
  props: z.object({ labels: z.array(z.string()), values: z.array(z.number()), title: z.string().optional() }), component: null,
});
export const LineChart = defineComponent({
  name: "LineChart", description: "A line over ordered labels (days, weeks, months).",
  props: z.object({ labels: z.array(z.string()), values: z.array(z.number()), title: z.string().optional() }), component: null,
});
export const Steps = defineComponent({
  name: "Steps", description: "A plan or progress list; each step carries a status.",
  props: z.object({ steps: z.array(z.object({ label: z.string(), status: z.enum(["todo", "doing", "done"]).optional() })) }), component: null,
});
export const Sources = defineComponent({
  name: "Sources", description: "The links a research answer rests on.",
  props: z.object({ sources: z.array(z.object({ title: z.string(), url: z.string() })) }), component: null,
});
export const Image = defineComponent({
  name: "Image", description: "A picture from THIS message's attachments, named by its file name (never a URL).",
  props: z.object({ fileName: z.string(), alt: z.string() }), component: null,
});
export const Choice = defineComponent({
  name: "Choice", description: "A question with options the person presses; the pick is posted as their reply. Not for sign-offs (those are approvals).",
  props: z.object({ question: z.string(), options: z.array(z.string()), multi: z.boolean().optional() }), component: null,
});
export const Button = defineComponent({
  name: "Button", description: "One labelled reply; pressing it posts the label as the person's message.",
  props: z.object({ label: z.string(), variant: z.enum(["primary", "secondary"]).optional() }), component: null,
});
export const Buttons = defineComponent({
  name: "Buttons", description: "A row of Buttons.",
  props: z.object({ buttons: z.array(Button.ref) }), component: null,
});
export const FollowUps = defineComponent({
  name: "FollowUps", description: "Suggested next replies, shown as small chips under the message.",
  props: z.object({ replies: z.array(z.string()) }), component: null,
});
export const Input = defineComponent({
  name: "Input", description: "A text field in a Form.",
  props: z.object({ name: z.string(), label: z.string(), placeholder: z.string().optional() }), component: null,
});
export const Select = defineComponent({
  name: "Select", description: "A pick-one field in a Form.",
  props: z.object({ name: z.string(), label: z.string(), options: z.array(z.string()) }), component: null,
});
export const DatePicker = defineComponent({
  name: "DatePicker", description: "A date field in a Form.",
  props: z.object({ name: z.string(), label: z.string() }), component: null,
});
export const Form = defineComponent({
  name: "Form", description: "Fields and a Submit; submitting posts one `Label: value` line per field as the person's reply.",
  props: z.object({ title: z.string(), fields: z.array(z.union([Input.ref, Select.ref, DatePicker.ref])), submit: z.string().optional() }), component: null,
});
export const Stack = defineComponent({
  name: "Stack", description: "The root of every card: its children, top to bottom.",
  props: z.object({
    children: z.array(z.union([
      Text.ref, Callout.ref, Metric.ref, Table.ref, EditableTable.ref, BarChart.ref, LineChart.ref,
      Steps.ref, Sources.ref, Image.ref, Choice.ref, Buttons.ref, FollowUps.ref, Form.ref,
    ])),
  }),
  component: null,
});

export const CARD_COMPONENTS = [
  Stack, Text, Callout, Metric, Table, Col, EditableTable, BarChart, LineChart, Steps,
  Sources, Image, Choice, Buttons, Button, FollowUps, Form, Input, Select, DatePicker,
] as const;

export type CardName = (typeof CARD_COMPONENTS)[number]["name"];

/** Prompt and parser source. Renderers come from the platform bindings, not from here. */
export const cardLibrary = createLibrary({ root: CARD_ROOT, components: [...CARD_COMPONENTS] });
