"use client";
import { useRouter } from "next/navigation";

export function SignOutButton() {
  const router = useRouter();
  return (
    <button
      data-testid="sign-out-button"
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
