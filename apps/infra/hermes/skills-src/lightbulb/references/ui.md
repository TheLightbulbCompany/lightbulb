# Cards in chat

A reply may carry ONE card: a fenced block tagged `openui-lang`. The app draws it as
buttons, a table, numbers, a plan or a form. A person's press on it arrives as their
next message, addressed to you, in plain words ("Studio", "Name: Ada", a table).

## When

Cards are the exception. A yes/no, a two-way question, a casual question or a status line is
just a sentence. Use a card only when the person must pick from three or more options, or
asked for numbers, a comparison or a plan with several items.

| You want to | Write |
|---|---|
| Ask the person to choose between things you can name | `Choice` (or `Buttons` for two or three labelled replies) |
| Offer the next things they might ask for | `FollowUps` |
| Show a number, a change, a KPI | `Metric` |
| Compare things side by side | `Table` |
| Have the person correct numbers or names you drafted | `EditableTable` |
| Show a plan or where work stands | `Steps` |
| Collect a few fields (a name, a plan, a date) | `Form` with `Input`, `Select`, `DatePicker` |
| Back a research answer with its links | `Sources` |
| Show a picture you attached in this reply | `Image` (by file name, never a URL) |
| Show how numbers move, when the shape is the point | `BarChart`, `LineChart` |

Not a card: a sign-off, a spend, a launch, a public post. Those are approvals, with the
`request_approval` tool. A `Choice` is for a quick pick in conversation.

## Rules

- One fence per reply, with a sentence or two of your own before it. Never a fence in a
  feed post, in mail, or in a thread under a message that is not yours.
- `root = Stack([...])` is always the first line. Arguments are positional, in the order
  of the signature; strings in double quotes; leave trailing optional arguments out. Never
  `name=value`: a multi-pick is `Choice("Which channels?", ["Email", "LinkedIn"], true)`.
- A `Choice` has two to six options of at most forty characters, and asks one thing. Put
  what you recommend first.
- Only what you can act on: never offer an option you would not do, and never a button
  that implies a record changed (nothing changes on a press; the person's words do).
- A card is not a document. Research, plans and reports still go in a document
  (`references/chat-vs-document.md`); a card may summarize one.

## Examples

```openui-lang
root = Stack([t, q])
t = Text("Two plans are ready. I'd start with Studio: it is where the demand was.")
q = Choice("Which plan should I price first?", ["Studio", "Starter", "Both at once"])
```

```openui-lang
root = Stack([m1, m2, tbl])
m1 = Metric("MRR", "$4,200", "+12% vs September")
m2 = Metric("Open invoices", "7", "3 overdue")
tbl = Table([Col("Customer", ["Acme", "Birch"]), Col("Due", ["Oct 2", "Oct 9"]), Col("Amount", ["$1,200", "$640"])])
```

```openui-lang
root = Stack([f])
f = Form("New customer", [name, plan], "Add")
name = Input("name", "Name", "Ada Lovelace")
plan = Select("plan", "Plan", ["Studio", "Starter"])
```

## Signatures

A child in `[...]` is
written inline (`Col("Plan", ["A"])`) or as a named line referenced by name.

<!-- openui:signatures:start -->
Arguments marked with ? are optional. Sub-components can be inline or referenced; prefer references for better streaming.

Stack(children: (Text | Callout | Metric | Table | EditableTable | BarChart | LineChart | Steps | Sources | Image | Choice | Buttons | FollowUps | Form)[]) — The root of every card: its children, top to bottom.
Text(text: string, size?: "body" | "small" | "heading") — A line or paragraph of prose.
Callout(text: string, tone?: "info" | "warn" | "good") — A short note that stands out: a caveat, a warning, a win.
Metric(label: string, value: string, delta?: string) — One number with its label and an optional change, e.g. Metric("MRR", "$4,200", "+12%").
Table(columns: Col[]) — A small table for a comparison; one Col per column, all the same length.
Col(label: string, data: (string | number)[]) — One table column: its header and its cells, top to bottom.
EditableTable(columns: string[], rows: string[][], submit?: string) — A table the person edits in place; Submit posts the rows back as their reply.
BarChart(labels: string[], values: number[], title?: string) — A bar chart, one bar per label. Only when the shape of the numbers is the point.
LineChart(labels: string[], values: number[], title?: string) — A line over ordered labels (days, weeks, months).
Steps(steps: {label: string, status?: "todo" | "doing" | "done"}[]) — A plan or progress list; each step carries a status.
Sources(sources: {title: string, url: string}[]) — The links a research answer rests on.
Image(fileName: string, alt: string) — A picture from THIS message's attachments, named by its file name (never a URL).
Choice(question: string, options: string[], multi?: boolean) — A question with options the person presses; the pick is posted as their reply. Not for sign-offs (those are approvals).
Buttons(buttons: Button[]) — A row of Buttons.
Button(label: string, variant?: "primary" | "secondary") — One labelled reply; pressing it posts the label as the person's message.
FollowUps(replies: string[]) — Suggested next replies, shown as small chips under the message.
Form(title: string, fields: (Input | Select | DatePicker)[], submit?: string) — Fields and a Submit; submitting posts one `Label: value` line per field as the person's reply.
Input(name: string, label: string, placeholder?: string) — A text field in a Form.
Select(name: string, label: string, options: string[]) — A pick-one field in a Form.
DatePicker(name: string, label: string) — A date field in a Form.
<!-- openui:signatures:end -->
