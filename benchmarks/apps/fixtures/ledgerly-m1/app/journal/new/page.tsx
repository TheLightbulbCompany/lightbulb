import { EntryForm } from "@/components/entry-form";
import { requireUser } from "@/lib/auth";
import { listAccounts } from "@/lib/data";

export default async function NewEntry() {
  const user = await requireUser();
  const accounts = (await listAccounts(user.id)).map((a) => ({ id: a.id, label: `${a.code} ${a.name}` }));
  return <EntryForm accounts={accounts} />;
}
