import { ImportForm } from "@/components/import-form";
import { requireUser } from "@/lib/auth";

export default async function ImportPage() {
  await requireUser();
  return <ImportForm />;
}
