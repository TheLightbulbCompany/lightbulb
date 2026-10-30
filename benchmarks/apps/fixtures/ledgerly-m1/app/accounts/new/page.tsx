import { AccountForm } from "@/components/account-form";
import { requireUser } from "@/lib/auth";

export default async function NewAccount() {
  await requireUser();
  return <AccountForm />;
}
