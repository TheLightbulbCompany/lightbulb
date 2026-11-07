import { NotAuthorized } from "@/components/not-authorized";
import { SettingsForm } from "@/components/settings-form";
import { requireUser } from "@/lib/auth";
import { membership } from "@/lib/data";

export default async function Settings({ params }: { params: Promise<{ orgId: string }> }) {
  const user = await requireUser();
  const m = await membership(user.id, (await params).orgId);
  if (!m) return <NotAuthorized />;
  return <SettingsForm orgId={m.org.id} name={m.org.name} description={m.org.description} />;
}
