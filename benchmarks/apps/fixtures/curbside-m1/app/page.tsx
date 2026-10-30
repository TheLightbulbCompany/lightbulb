import { redirect } from "next/navigation";
import { requireUser } from "@/lib/auth";

export default async function Home() {
  await requireUser(); // signed out: to sign-in
  redirect("/restaurants");
}
