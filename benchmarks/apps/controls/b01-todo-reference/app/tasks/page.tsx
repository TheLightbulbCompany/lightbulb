import Link from "next/link";
import { StatusFilter } from "@/components/status-filter";
import { requireUser } from "@/lib/auth";
import { listTasks } from "@/lib/tasks";

export default async function Tasks({ searchParams }: { searchParams: Promise<{ status?: string }> }) {
  const user = await requireUser();
  const { status = "all" } = await searchParams;
  const rows = await listTasks(user.id, status);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Your tasks</h1>
        <div className="flex items-center gap-3">
          <StatusFilter value={status} />
          <Link href="/tasks/new" data-testid="task-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">New task</Link>
        </div>
      </div>
      {rows.length === 0 ? (
        <p data-testid="tasks-empty" className="text-slate-500">No tasks yet.</p>
      ) : (
        <ul data-testid="tasks-list" className="divide-y rounded border bg-white">
          {rows.map((t) => (
            <li key={t.id} data-testid="task-row" className="flex items-center justify-between px-4 py-3">
              <span data-testid="task-row-title" className={t.done ? "text-slate-400 line-through" : ""}>{t.title}</span>
              <span className="flex items-center gap-3 text-sm text-slate-500">
                {t.priority}{t.dueDate && ` · due ${t.dueDate}`}
                <Link href={`/tasks/${t.id}`} data-testid="task-row-link" className="underline">Open</Link>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
