"use client";
import { useRouter } from "next/navigation";

export function TaskActions({ id, done }: { id: string; done: boolean }) {
  const router = useRouter();
  const btn = "rounded border px-3 py-1.5";
  async function patch(done: boolean) {
    await fetch(`/api/tasks/${id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ done }) });
    router.refresh();
  }
  async function remove() {
    await fetch(`/api/tasks/${id}`, { method: "DELETE" });
    router.push("/tasks");
    router.refresh();
  }
  return (
    <div className="flex gap-2">
      {done
        ? <button data-testid="task-reopen-button" className={btn} onClick={() => patch(false)}>Reopen</button>
        : <button data-testid="task-complete-button" className={btn} onClick={() => patch(true)}>Mark done</button>}
      <button data-testid="task-delete-button" className={btn} onClick={remove}>Delete</button>
    </div>
  );
}
