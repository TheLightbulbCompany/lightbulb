import { SimpleForm } from "@/components/simple-form";
import { requireUser } from "@/lib/auth";

export default async function NewService() {
  await requireUser();
  return <SimpleForm kind="services" title="New service" submitTestid="service-form-submit" fields={[
    { name: "name", label: "Name", testid: "service-form-name" },
    { name: "durationMinutes", label: "Length in minutes", testid: "service-form-duration", type: "number" },
  ]} />;
}
