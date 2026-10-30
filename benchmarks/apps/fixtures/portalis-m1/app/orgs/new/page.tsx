import { CreateOrgForm } from "@/components/create-org-form";
import { requireUser } from "@/lib/auth";

export default async function NewOrg() {
  await requireUser();
  return <CreateOrgForm />;
}
