"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

const input = "w-full rounded border px-3 py-2";

export function TaskForm() {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, string>;
    if (!b.title.trim()) return setError("Title is required");
    const r = await fetch("/api/tasks", { method: "POST",  headers: { "content-type": "application/json" }, body: JSON.stringify(b) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push("/tasks");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">New task</h1>
      <input name="title" placeholder="Title" data-testid="task-form-title" className={input} />
      <textarea name="note" placeholder="Note (optional)" className={input} />
      <select name="priority" defaultValue="medium" data-testid="task-form-priority" className={input}>
        <option value="low">low</option>
        <option value="medium">medium</option>
        <option value="high">high</option>
      </select>
      <input name="dueDate" type="date" className={input} />
      {error && <p role="alert" data-testid="task-form-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="task-form-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Save task</button>
    </form>
  );
}
