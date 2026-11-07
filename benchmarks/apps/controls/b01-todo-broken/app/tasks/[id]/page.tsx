import { notFound } from "next/navigation";
import { TaskActions } from "@/components/task-actions";
import { getTask } from "@/lib/tasks";

export default async function TaskPage({ params }: { params: Promise<{ id: string }> }) {
  
  const t = await getTask(undefined, (await params).id);
  if (!t) notFound();
  return (
    <div className="space-y-3">
      <h1 data-testid="task-detail-title" className="text-xl font-semibold">{t.title}</h1>
      <p>Status: <span data-testid="task-detail-status">{t.done ? "done" : "open"}</span></p>
      <p className="text-sm text-slate-500">Priority {t.priority}{t.dueDate && ` · due ${t.dueDate}`}</p>
      {t.note && <p>{t.note}</p>}
      <TaskActions id={t.id} done={t.done} />
    </div>
  );
}
