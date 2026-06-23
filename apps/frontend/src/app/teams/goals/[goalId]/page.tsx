"use client";

import { useParams } from "next/navigation";
import { GoalDetailPanel } from "@/components/teams/panels/GoalDetailPanel";

export default function Page() {
  const { goalId } = useParams<{ goalId: string }>();
  return <GoalDetailPanel goalId={goalId!} />;
}
