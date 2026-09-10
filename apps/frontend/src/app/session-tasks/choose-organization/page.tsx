import { TaskChooseOrganization } from "@clerk/nextjs";

export default function ChooseOrganizationTaskPage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <TaskChooseOrganization redirectUrlComplete="/chat" />
    </main>
  );
}
