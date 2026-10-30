import { Buffer } from "node:buffer";

import { crmDocumentBase64 } from "@/lib/crm-document";

export default function Home() {
  const crmDocument = Buffer.from(crmDocumentBase64, "base64").toString("utf8");

  return (
    <main className="h-screen w-full overflow-hidden bg-slate-950">
      <iframe
        className="h-full w-full border-0"
        srcDoc={crmDocument}
        title="Lightbulb CRM"
      />
    </main>
  );
}
