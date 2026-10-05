import { ThemeStamp } from "@/components/ThemeStamp";

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <ThemeStamp />
      {children}
    </>
  );
}
