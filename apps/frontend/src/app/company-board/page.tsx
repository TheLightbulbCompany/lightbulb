import type React from "react";

// Read the backend URL inline rather than importing `@/lib/api`: that module
// re-exports @isol8/shared, whose SWR-based hooks have no default export in
// React's server build, which breaks this Server Component at build time.
const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

type BoardIssue = {
  id: string;
  identifier?: string;
  title?: string;
  status?: string;
  priority?: string;
  assigneeAgentId?: string;
  responsibleUserId?: string;
  workProducts?: Array<{ id?: string; title?: string; url?: string; status?: string }>;
};

type BoardRoutine = {
  id: string;
  title?: string;
  status?: string;
  enabled?: boolean;
  reviewerAgentId?: string;
  reviewerUserId?: string;
  nextRunAt?: string;
};

type BoardRun = {
  id: string;
  issueId?: string;
  routineId?: string;
  agentId?: string;
  status?: string;
  startedAt?: string;
  finishedAt?: string;
};

type BoardPayload = {
  title: string;
  generatedAt: string;
  disclosureBoundary: { included: string[]; excluded: string[] };
  issues: BoardIssue[];
  routines: BoardRoutine[];
  runs: BoardRun[];
};

export const dynamic = "force-dynamic";
export const revalidate = 0;

async function loadBoard(): Promise<BoardPayload | null> {
  const response = await fetch(`${BACKEND_URL}/public/board`, { cache: "no-store" });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("Unable to load public board");
  return response.json();
}

function Chip({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full border border-slate-300 px-2 py-1 text-xs text-slate-700">{children}</span>;
}

export default async function CompanyBoardPage() {
  const board = await loadBoard();
  if (!board) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-20 text-slate-900">
        <h1 className="text-4xl font-semibold">Isol8 company board</h1>
        <p className="mt-4 text-slate-600">
          The public board is implemented but not published yet. It becomes available here after founder approval of the disclosure boundary.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-12 text-slate-950">
      <section className="rounded-3xl bg-slate-950 p-8 text-white shadow-xl">
        <p className="text-sm uppercase tracking-[0.25em] text-cyan-300">Live read-only view</p>
        <h1 className="mt-3 text-4xl font-semibold">{board.title}</h1>
        <p className="mt-4 max-w-3xl text-slate-300">
          Snapshot generated {new Date(board.generatedAt).toLocaleString()}. This page exposes operational status only: no comments, private documents, attachments, secrets, customer data, or write controls.
        </p>
      </section>

      <section className="mt-8 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl border border-slate-200 p-5">
          <h2 className="font-semibold">Included</h2>
          <div className="mt-3 flex flex-wrap gap-2">{board.disclosureBoundary.included.map((item) => <Chip key={item}>{item}</Chip>)}</div>
        </div>
        <div className="rounded-2xl border border-slate-200 p-5">
          <h2 className="font-semibold">Excluded</h2>
          <div className="mt-3 flex flex-wrap gap-2">{board.disclosureBoundary.excluded.map((item) => <Chip key={item}>{item}</Chip>)}</div>
        </div>
      </section>

      <section className="mt-10">
        <h2 className="text-2xl font-semibold">Issues</h2>
        <div className="mt-4 grid gap-4">
          {board.issues.map((issue) => (
            <article key={issue.id} className="rounded-2xl border border-slate-200 p-5">
              <div className="flex flex-wrap items-center gap-2">
                {issue.identifier ? <Chip>{issue.identifier}</Chip> : null}
                {issue.status ? <Chip>{issue.status}</Chip> : null}
                {issue.priority ? <Chip>{issue.priority}</Chip> : null}
              </div>
              <h3 className="mt-3 text-lg font-medium">{issue.title}</h3>
              <p className="mt-2 text-sm text-slate-500">Assignee: {issue.assigneeAgentId ?? "unassigned"} · Reviewer/user owner: {issue.responsibleUserId ?? "none"}</p>
              {issue.workProducts?.length ? (
                <ul className="mt-3 list-disc pl-5 text-sm">
                  {issue.workProducts.map((product) => (
                    <li key={product.id ?? product.title}>
                      {product.url ? <a className="text-blue-700 underline" href={product.url}>{product.title ?? product.url}</a> : product.title}
                    </li>
                  ))}
                </ul>
              ) : null}
            </article>
          ))}
        </div>
      </section>

      <section className="mt-10 grid gap-6 lg:grid-cols-2">
        <div>
          <h2 className="text-2xl font-semibold">Routines</h2>
          <div className="mt-4 space-y-3">
            {board.routines.map((routine) => (
              <article key={routine.id} className="rounded-2xl border border-slate-200 p-4">
                <h3 className="font-medium">{routine.title}</h3>
                <p className="text-sm text-slate-500">{routine.status ?? "unknown"} · reviewer {routine.reviewerAgentId ?? routine.reviewerUserId ?? "none"}</p>
              </article>
            ))}
          </div>
        </div>
        <div>
          <h2 className="text-2xl font-semibold">Runs</h2>
          <div className="mt-4 space-y-3">
            {board.runs.map((run) => (
              <article key={run.id} className="rounded-2xl border border-slate-200 p-4">
                <h3 className="font-medium">{run.status ?? "unknown"}</h3>
                <p className="text-sm text-slate-500">{run.issueId ?? run.routineId ?? run.agentId ?? run.id}</p>
              </article>
            ))}
          </div>
        </div>
      </section>
    </main>
  );
}
