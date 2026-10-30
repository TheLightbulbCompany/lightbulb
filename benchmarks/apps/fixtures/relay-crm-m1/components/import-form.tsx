"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

export function ImportForm() {
  const router = useRouter();
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setResult(""); setError("");
    const file = (e.currentTarget.elements.namedItem("file") as HTMLInputElement).files?.[0];
    if (!file) return setError("Choose a CSV file first");
    const r = await fetch("/api/contacts/import", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ csv: await file.text() }) });
    const body = await r.json().catch(() => null);
    if (!r.ok) return setError(body?.error ?? "Something went wrong");
    setResult(`${body.imported} imported, ${body.skipped} skipped`);
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">Import contacts</h1>
      <p className="text-sm text-slate-500">A CSV with the header row name,email,phone,title,company.</p>
      <input name="file" type="file" accept=".csv,text/csv" data-testid="import-file" />
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="import-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Import</button>
      {result && <p data-testid="import-result" className="text-green-700">{result}</p>}
    </form>
  );
}
