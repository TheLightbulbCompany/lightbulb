"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

// The sign-in <-> sign-up switch, carrying the page's `redirect_url` so the person still lands
// where they were sent from.
function Switch({ to, children }: { to: string; children: React.ReactNode }) {
  const back = useSearchParams().get("redirect_url");
  return <Link href={back ? `${to}?${new URLSearchParams({ redirect_url: back })}` : to}>{children}</Link>;
}

export function AuthSwitchLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <Suspense fallback={<Link href={to}>{children}</Link>}>
      <Switch to={to}>{children}</Switch>
    </Suspense>
  );
}
