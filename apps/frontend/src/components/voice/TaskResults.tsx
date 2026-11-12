"use client";

// What a task under an assistant message made (`taskRefs`): its status, the files and links it produced, and its full
// answer when the chat line only carries a summary. Files open in a new tab (a short-lived signed address); there is no
// task pane on the voice pages. The old room's MessageTasks (components/tasks) still draws the full chips there.
import type { ReactElement } from "react";
import { useAuth } from "@clerk/nextjs";
import { chipState, useTask, type CanonicalRoomMessage, type TaskOutput } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { MarkdownView } from "./MarkdownView";
import { cardMarkdown } from "./cardMarkdown";

export function TaskResults({ message }: { message: CanonicalRoomMessage }): ReactElement | null {
  const refs = message.taskRefs ?? [];
  if (!refs.length) return null;
  return <div className="vx-tasks" aria-label="Tasks in this message">
    {refs.map((r) => <TaskResult key={r.taskId} taskId={r.taskId} shown={message.text} />)}
  </div>;
}

// ponytail: one GET /tasks/{id} per ref (SWR dedupes); read the room's task list if chats grow many tasks.
function TaskResult({ taskId, shown }: { taskId: string; shown: string }): ReactElement | null {
  const { tasks } = useLiveEvents();
  const { userId } = useAuth();
  const { data } = useTask(tasks, taskId);
  if (!data) return null;
  const { task } = data;
  // The word only ("Done", "Working"): the clock beside it is not drawn here, so any fixed time will do.
  const state = chipState(task, userId ?? "", 0);
  const outputs = (task.outputs ?? []).filter((o) => !o.id.startsWith("text:"));
  const answer = state.mark === "done" ? data.attempts.at(-1)?.output?.trim() : undefined;
  return <div className="vx-task" data-task={taskId}>
    <span className="vx-task-line"><b>{task.title}</b> · {state.word}</span>
    {!!outputs.length && <ul className="vx-task-outputs">{outputs.map((o) => <li key={o.id}><Output output={o} /></li>)}</ul>}
    {answer && answer !== shown.trim() && <details className="vx-task-answer"><summary>Full answer</summary>
      <MarkdownView skipHtml components={cardMarkdown}>{answer}</MarkdownView></details>}
  </div>;
}

function Output({ output: o }: { output: TaskOutput }): ReactElement {
  const { messaging } = useLiveEvents();
  const ref = o.ref ?? {};
  if ((o.kind === "link" || o.kind === "pr") && /^https?:\/\//.test(ref.url ?? "")) {
    return <a className="vx-link" href={ref.url} target="_blank" rel="noopener noreferrer">{o.title}</a>;
  }
  if ((o.kind === "file" || o.kind === "document") && ref.roomId && ref.attachmentId) {
    const open = async () => {
      // The tab opens on the press (a popup blocker allows that), then goes to the signed address once it is known.
      const tab = window.open("", "_blank");
      try {
        const { url } = await messaging.attachmentUrl(ref.roomId, ref.attachmentId);
        if (tab) { tab.opener = null; tab.location.href = url; } else window.location.assign(url);
      } catch {
        tab?.close();
      }
    };
    return <button type="button" className="vx-link" onClick={() => void open()}>{o.title}</button>;
  }
  return <span>{o.title}</span>;
}
