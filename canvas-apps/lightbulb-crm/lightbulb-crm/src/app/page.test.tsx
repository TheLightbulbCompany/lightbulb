import { Buffer } from "node:buffer";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import Home from "./page";
import { crmDocumentBase64 } from "@/lib/crm-document";

const crmDocument = Buffer.from(crmDocumentBase64, "base64").toString("utf8");

describe("Lightbulb CRM Canvas app", () => {
  it("renders the CRM inside the native app page", () => {
    const markup = renderToStaticMarkup(<Home />);

    expect(markup).toContain('title="Lightbulb CRM"');
    expect(markup).toContain("Lightbulb CRM");
    expect(markup).toContain("Pipeline");
    expect(markup).toContain("Contacts");
  });

  it("preserves the requested CRM workflows", () => {
    expect(crmDocument).toContain("localStorage.getItem('lightbulb-crm')");
    expect(crmDocument).toContain('id="search"');
    expect(crmDocument).toContain('id="ownerFilter"');
    expect(crmDocument).toContain('id="sourceFilter"');
    expect(crmDocument).toContain("function renderMetrics()");
    expect(crmDocument).toContain("function openContact(id)");
    expect(crmDocument).toContain("contacts.unshift(obj)");
    expect(crmDocument).toContain("contacts=contacts.filter(c=>c.id!==id)");
    expect(crmDocument).toContain("a.download='lightbulb-crm.json'");
    expect(crmDocument).toContain("contacts=d;save()");
    expect(crmDocument).toContain("Next follow-up");
  });
});
