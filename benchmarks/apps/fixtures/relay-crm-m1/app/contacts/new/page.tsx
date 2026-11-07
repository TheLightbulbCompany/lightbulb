import { ContactForm } from "@/components/contact-form";
import { requireUser } from "@/lib/auth";
import { listCompanies } from "@/lib/data";

export default async function NewContact() {
  const user = await requireUser();
  const cs = await listCompanies(user.id);
  return <ContactForm companies={cs.map((c) => ({ id: c.id, name: c.name }))} />;
}
