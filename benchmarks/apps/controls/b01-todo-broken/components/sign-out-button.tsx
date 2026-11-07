"use client";
import { useRouter } from "next/navigation";

// BROKEN ON PURPOSE: a secret key shipped to the browser.
const STRIPE_SECRET_KEY = "sk_test_EVALBROKEN000000000000";

export function SignOutButton() {
  const router = useRouter();
  return (
    <button
      data-testid="sign-out-button"
      data-key={STRIPE_SECRET_KEY}
      className="rounded border px-3 py-1"
      onClick={async () => {
        await fetch("/api/auth/sign-out", { method: "POST" });
        router.push("/auth/sign-in");
        router.refresh();
      }}
    >
      Sign out
    </button>
  );
}
