"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

const HOME = "/bookings";
const input = "w-full rounded border px-3 py-2";

export function AuthForm({ mode }: { mode: "signup" | "signin" }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const up = mode === "signup";
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.currentTarget));
    const r = await fetch(up ? "/api/auth/sign-up" : "/api/auth/sign-in", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push(HOME);
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">{up ? "Create your account" : "Sign in"}</h1>
      {up && <input name="name" placeholder="Name" data-testid="signup-name" className={input} />}
      <input name="email" type="email" placeholder="Email" data-testid={up ? "signup-email" : "signin-email"} className={input} />
      <input name="password" type="password" placeholder="Password" data-testid={up ? "signup-password" : "signin-password"} className={input} />
      {error && <p role="alert" data-testid={up ? "signup-error" : "signin-error"} className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid={up ? "signup-submit" : "signin-submit"} className="rounded bg-slate-900 px-4 py-2 text-white">{up ? "Sign up" : "Sign in"}</button>
      <p className="text-sm"><Link href={up ? "/auth/sign-in" : "/auth/sign-up"} className="underline">{up ? "Have an account? Sign in" : "New here? Sign up"}</Link></p>
    </form>
  );
}
