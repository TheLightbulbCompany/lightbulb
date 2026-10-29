import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { DocsNav } from "@/components/docs/DocsNav";
import { getAllDocs } from "@/content/docs";

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  const items = getAllDocs().map(({ slug, title, section }) => ({ slug, title, section }));
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="blog-wrap docs-wrap docs-layout">
        <DocsNav items={items} />
        <div className="docs-main">{children}</div>
      </section>
      <Footer />
    </main>
  );
}
