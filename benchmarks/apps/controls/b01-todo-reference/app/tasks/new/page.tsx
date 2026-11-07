import { TaskForm } from "@/components/task-form";
import { requireUser } from "@/lib/auth";

export default async function NewTask() {
  await requireUser();
  return <TaskForm />;
}
