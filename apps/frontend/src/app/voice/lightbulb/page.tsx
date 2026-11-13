import { LightbulbPage } from "@/components/voice/LightbulbPage";

export default async function Page({ searchParams }: { searchParams: Promise<{ room?: string | string[] }> }) {
  const { room } = await searchParams;
  return <LightbulbPage room={typeof room === "string" ? room : undefined} />;
}
