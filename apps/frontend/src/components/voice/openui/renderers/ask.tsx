"use client";

import { useId, useState } from "react";
import { CARD_MAX_OPTIONS, answerIsFormOf, answerIsFrom, choiceReply, formReply } from "@lightbulb/shared";
import { Button as UiButton } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input as UiInput } from "@/components/ui/input";
import { useCardPress } from "./shared";
import type { RenderProps, SubComponent } from "./shared";

export function ChoiceView({ props }: RenderProps<{ question: string; options: string[]; multi?: boolean }>) {
  const { press, disabled, answered, retry } = useCardPress();
  const [picked, setPicked] = useState<string[]>([]);
  const shown = props.options.slice(0, CARD_MAX_OPTIONS);
  const hidden = props.options.length - shown.length;
  const answeredSet = new Set((answered ?? "").split(",").map((s) => s.trim()));
  return (
    <div role="group" aria-label={props.question} className="grid gap-2 openui-live">
      <p className="text-ui font-medium">{props.question}</p>
      {props.multi ? (
        <>
          {shown.map((o) => {
            const checked = answered !== null ? answeredSet.has(o) : picked.includes(o);
            return <label key={o} className="flex items-center gap-2 text-ui">
              <Checkbox checked={checked} disabled={disabled || retry !== null} aria-label={o}
                onCheckedChange={(v) => setPicked((cur) => v ? [...cur, o] : cur.filter((p) => p !== o))} />
              {o}
            </label>;
          })}
          {answered === null && <UiButton type="button" size="sm" className="justify-self-start" disabled={disabled || picked.length === 0} onClick={() => press(choiceReply(picked))}>Done</UiButton>}
        </>
      ) : (
        <div className="flex flex-wrap gap-2">
          {shown.map((o) => <UiButton key={o} type="button" size="sm" variant="outline" disabled={disabled || (retry !== null && retry !== o)} aria-pressed={answered !== null && answered === o} onClick={() => press(o)}>{o}</UiButton>)}
        </div>
      )}
      {hidden > 0 && <p className="text-caption text-muted-foreground">+{hidden} more</p>}
      {answerIsFrom(answered, props.options, props.multi) && <p className="text-caption openui-answer">You picked {answered}.</p>}
    </div>
  );
}

type ButtonProps = { label: string; variant?: "primary" | "secondary" };

export function ButtonsView({ props }: RenderProps<{ buttons: SubComponent<ButtonProps>[] }>) {
  const { press, disabled, answered, retry } = useCardPress();
  return (
    <div className="flex flex-wrap gap-2 openui-live" role="group">
      {props.buttons.map((b, i) => {
        const text = b.props.label.trim();
        return <UiButton key={`${b.props.label}-${i}`} type="button" size="sm" variant={b.props.variant === "secondary" ? "outline" : "default"}
          disabled={disabled || (retry !== null && retry !== text)} aria-pressed={answered !== null && answered === text} onClick={() => press(text)}>{b.props.label}</UiButton>;
      })}
      {answerIsFrom(answered, props.buttons.map((b) => b.props.label)) && <p className="text-caption openui-answer">You picked {answered}.</p>}
    </div>
  );
}

/** Button never renders alone: Buttons reads its props. */
export function ButtonView() { return null; }

export function FollowUpsView({ props }: RenderProps<{ replies: string[] }>) {
  const { press, disabled, answered, retry } = useCardPress();
  return (
    <div className="flex flex-wrap gap-2 openui-live" role="group" aria-label="Suggested replies">
      {props.replies.map((r) => <UiButton key={r} type="button" size="sm" variant="outline" disabled={disabled || (retry !== null && retry !== r)}
        aria-pressed={answered !== null && answered === r} onClick={() => press(r)}>{r}</UiButton>)}
      {answerIsFrom(answered, props.replies) && <p className="text-caption openui-answer">You picked {answered}.</p>}
    </div>
  );
}

type FieldProps = { name: string; label: string; placeholder?: string; options?: string[] };

export function FormView({ props }: RenderProps<{ title: string; fields: SubComponent<FieldProps>[]; submit?: string }>) {
  const { press, disabled, answered, retry } = useCardPress();
  // While an unanswered submit is being retried, the fields freeze: the retry replays what was sent, not an edit.
  const frozen = disabled || retry !== null;
  const base = useId();
  const [values, setValues] = useState<Record<string, string>>({});
  const set = (name: string, value: string) => setValues((cur) => ({ ...cur, [name]: value }));
  const submit = () => press(formReply(props.fields.map((f) => ({ label: f.props.label, value: values[f.props.name] ?? "" }))));
  return (
    <form className="grid gap-2 openui-live" aria-label={props.title} onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <p className="text-ui font-medium">{props.title}</p>
      {props.fields.map((f) => {
        const id = `${base}-${f.props.name}`;
        const value = values[f.props.name] ?? "";
        return <div key={f.props.name} className="grid gap-1">
          <label htmlFor={id} className="text-caption text-muted-foreground">{f.props.label}</label>
          {f.typeName === "Select"
            ? <select id={id} className="room-input" value={value} disabled={frozen} onChange={(e) => set(f.props.name, e.target.value)}>
                <option value="">Choose…</option>
                {(f.props.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            : f.typeName === "DatePicker"
              ? <UiInput id={id} type="date" value={value} disabled={frozen} onChange={(e) => set(f.props.name, e.target.value)} />
              : <UiInput id={id} type="text" placeholder={f.props.placeholder} value={value} disabled={frozen} onChange={(e) => set(f.props.name, e.target.value)} />}
        </div>;
      })}
      {answerIsFormOf(answered, props.fields.map((f) => f.props.label))
        ? <p className="text-caption openui-answer">Sent.</p>
        : <UiButton type="submit" size="sm" className="justify-self-start" disabled={disabled}>{props.submit ?? "Submit"}</UiButton>}
    </form>
  );
}

/** Fields never render alone: Form reads their props. */
export function InputView() { return null; }
export function SelectView() { return null; }
export function DatePickerView() { return null; }
