"use client";

import { useParams } from "next/navigation";
import { RoutineDetailPanel } from "@/components/teams/panels/RoutineDetailPanel";

export default function Page() {
  const { routineId } = useParams<{ routineId: string }>();
  return <RoutineDetailPanel routineId={routineId!} />;
}
