"use client";

import { useParams } from "next/navigation";
import { ApprovalDetailPanel } from "@/components/teams/panels/ApprovalDetailPanel";

export default function Page() {
  const { approvalId } = useParams<{ approvalId: string }>();
  return <ApprovalDetailPanel approvalId={approvalId!} />;
}
