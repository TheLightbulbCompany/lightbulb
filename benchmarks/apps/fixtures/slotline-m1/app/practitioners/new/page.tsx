import { SimpleForm } from "@/components/simple-form";
import { requireUser } from "@/lib/auth";

export default async function NewPractitioner() {
  await requireUser();
  return <SimpleForm kind="practitioners" title="New practitioner" submitTestid="practitioner-form-submit" fields={[
    { name: "name", label: "Name", testid: "practitioner-form-name" },
    { name: "specialty", label: "Specialty", testid: "practitioner-form-specialty" },
  ]} />;
}
