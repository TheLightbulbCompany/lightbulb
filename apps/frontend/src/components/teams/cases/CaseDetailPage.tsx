// apps/frontend/src/components/teams/cases/CaseDetailPage.tsx

// Ported from upstream Paperclip's CaseDetail (paperclip/ui/src/pages/CaseDetail.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Header, relationships, the Overview / Properties / Activity tabs and the
// property sections are upstream's. Swapped: TanStack Query -> useTeamsApi
// (SWR, polled — cases emit no live events); breadcrumb context -> an inline
// breadcrumb like GoalDetailPage; upstream's PanelContext side panel -> the
// same [1fr 320px] aside the issue/goal detail pages use (so the More menu's
// "Properties" item, which only re-opened that panel, is gone); the label
// popover -> Teams' LabelPicker; and upstream's IssueDocumentsSection ->
// Teams' own issue-document review row (DocumentRow) pointed at the case
// document routes, so case documents are read + reviewed here, not edited.

"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { Check, ChevronRight, Copy, MoreVertical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DocumentRow } from "@/components/teams/issues/DocumentRow";
import { LabelPicker } from "@/components/teams/issues/pickers/LabelPicker";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import type {
  CaseDetail,
  CaseParentRef,
  CaseStatus,
  CaseSummary,
  CompanyAgent,
  IssueDocument,
} from "@/components/teams/shared/types";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick } from "@/lib/shellNav";
import { notifyApiError } from "@/lib/notify";
import { cn } from "@/lib/utils";
import { CaseActivityFeed } from "./CaseActivityFeed";
import { CaseAttachmentsGallery } from "./CaseAttachmentsGallery";
import { CaseChildrenTree } from "./CaseChildrenTree";
import { CaseFieldValue, IssueReferencePill } from "./CaseFieldsPanel";
import { CaseCopyableToken, CaseIdentifierKey } from "./CaseIdentifierKey";
import { CaseStatusPicker } from "./CaseStatusBadge";
import { caseHref } from "./caseNav";
import { useCaseChildren, useCaseDetail, useCaseEvents, useCaseMutations } from "./hooks/useCases";

const STATUS_LABEL: Record<CaseStatus, string> = {
  draft: "Draft",
  in_progress: "In progress",
  in_review: "In review",
  approved: "Approved",
  done: "Done",
  cancelled: "Cancelled",
};

const PRIMARY_FIELD_KEYS = ["name", "title", "body", "description"] as const;

type CasePropertyDisplayMode = "compact" | "full";

function fieldValueByName(fields: Record<string, unknown>, name: string): unknown {
  return fields[name] ?? fields[name.charAt(0).toUpperCase() + name.slice(1)];
}

function caseFieldKeyVariants(key: string): string[] {
  if (!key) return [key];
  return [key, key.charAt(0).toUpperCase() + key.slice(1), key.charAt(0).toLowerCase() + key.slice(1)];
}

function hasFieldValue(value: unknown): boolean {
  return value !== null && value !== undefined && !(typeof value === "string" && value.trim() === "");
}

function casePropertyRows(caseData: CaseDetail): { key: string; label: string; value: unknown }[] {
  const reservedKeys = new Set<string>(PRIMARY_FIELD_KEYS.flatMap((key) => caseFieldKeyVariants(key)));
  const documentKeys = new Set(caseData.documents.flatMap((documentRef) => caseFieldKeyVariants(documentRef.key)));
  const primary = PRIMARY_FIELD_KEYS.filter(
    (key) => !caseFieldKeyVariants(key).some((variant) => documentKeys.has(variant)),
  )
    .map((key) => ({
      key,
      label: key,
      value: key === "title" ? fieldValueByName(caseData.fields, key) ?? caseData.title : fieldValueByName(caseData.fields, key),
    }))
    .filter((row) => hasFieldValue(row.value));

  const generic = Object.entries(caseData.fields)
    .filter(([key]) => !reservedKeys.has(key) && !documentKeys.has(key))
    .map(([key, value]) => ({ key, label: key, value }));

  return [...primary, ...generic];
}

/** The case's documents in the shape Teams' issue-document review row reads. */
function caseDocuments(caseData: CaseDetail): (IssueDocument & { latestRevisionId: string | null })[] {
  return caseData.documents.map(({ key, document }) => ({
    id: document.id,
    key,
    title: document.title,
    format: document.format,
    body: document.latestBody ?? "",
    updatedByAgentId: document.updatedByAgentId,
    updatedByUserId: document.updatedByUserId,
    updatedAt: document.updatedAt,
    createdAt: document.createdAt,
    latestRevisionNumber: document.latestRevisionNumber,
    // Carried on the wire, read by DocumentRow, absent from the slim type.
    latestRevisionId: document.latestRevisionId,
  }));
}

function CaseRelationshipsSection({
  parent,
  childCases,
}: {
  parent: CaseParentRef | null;
  childCases: CaseSummary[];
}) {
  if (!parent && childCases.length === 0) return null;

  return (
    <section className="space-y-3" aria-label="Case relationships">
      {parent ? (
        <div className="space-y-1">
          <h2 className="text-caption font-medium text-[var(--fg-mute)]">Parent</h2>
          <CaseChildrenTree cases={[parent]} />
        </div>
      ) : null}
      {childCases.length > 0 ? (
        <div className="space-y-1">
          <h2 className="text-caption font-medium text-[var(--fg-mute)]">Children {childCases.length}</h2>
          <CaseChildrenTree cases={childCases} maxVisible={5} />
        </div>
      ) : null}
    </section>
  );
}

/** Stand-ins for upstream's issue-properties PropertySection / PropertyChip. */
function PropertySection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-1 border-t border-[var(--edge)] pt-3 first:border-t-0 first:pt-0">
      <h3 className="text-caption font-semibold uppercase tracking-wide text-[var(--fg-mute)]">{title}</h3>
      {children}
    </div>
  );
}

function PropertyChip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-[var(--edge)] px-2 py-0.5 text-caption text-[var(--fg-soft)]">
      {children}
    </span>
  );
}

function CasePropertyRow({
  label,
  children,
  wrap,
  mode,
}: {
  label: string;
  children: ReactNode;
  wrap?: boolean;
  mode: CasePropertyDisplayMode;
}) {
  return (
    <div
      className={cn("flex w-full min-w-0 gap-3 py-1", wrap ? "items-start" : "items-center")}
      data-property-row="true"
    >
      <span
        className={cn(
          "shrink-0 break-words text-caption text-[var(--fg-mute)]",
          mode === "compact" ? "w-20" : "w-40",
          wrap && "mt-0.5",
        )}
        data-property-label={label}
      >
        {label}
      </span>
      <div className={cn("flex min-w-0 flex-1 items-center gap-1.5", wrap && "flex-wrap")}>{children}</div>
    </div>
  );
}

/** The case's properties: the aside (compact) and the Properties tab (full). */
function CasePropertiesContent({
  caseData,
  childCases,
  labelsPending,
  onLabelIdsChange,
  mode,
}: {
  caseData: CaseDetail;
  childCases: CaseSummary[];
  labelsPending?: boolean;
  onLabelIdsChange: (labelIds: string[]) => void;
  mode: CasePropertyDisplayMode;
}) {
  const propertyRows = casePropertyRows(caseData);
  const isFull = mode === "full";

  return (
    <div className={cn("space-y-4", isFull && "space-y-6")}>
      <PropertySection title="Case">
        <CasePropertyRow label="Type" mode={mode}>
          <PropertyChip>{caseData.caseType}</PropertyChip>
        </CasePropertyRow>
        {caseData.key ? (
          <CasePropertyRow label="Key" mode={mode}>
            <CaseCopyableToken
              value={caseData.key}
              label="case key"
              className="font-mono text-caption text-[var(--fg-mute)]"
              truncate={!isFull}
            />
          </CasePropertyRow>
        ) : null}
        <CasePropertyRow label="Labels" wrap mode={mode}>
          <LabelPicker selected={caseData.labels} onChange={onLabelIdsChange} />
          {labelsPending ? <span className="text-caption text-[var(--fg-mute)]">Saving...</span> : null}
        </CasePropertyRow>
      </PropertySection>

      {propertyRows.length > 0 ? (
        <PropertySection title="Fields">
          {propertyRows.map(({ key, label, value }) => (
            <CasePropertyRow
              key={key}
              label={label}
              wrap={isFull || Array.isArray(value) || (typeof value === "object" && value !== null)}
              mode={mode}
            >
              <span className={cn("min-w-0 text-ui", !isFull && "truncate")}>
                <CaseFieldValue value={value} fieldKey={key} variant={mode} />
              </span>
            </CasePropertyRow>
          ))}
        </PropertySection>
      ) : null}

      <PropertySection title="Linked tasks">
        {caseData.issueLinks.length === 0 ? (
          <CasePropertyRow label="Tasks" mode={mode}>
            <span className="text-caption text-[var(--fg-mute)]">None yet</span>
          </CasePropertyRow>
        ) : (
          <CasePropertyRow label="Tasks" wrap mode={mode}>
            <div className="flex flex-wrap items-center gap-1.5">
              {caseData.issueLinks.map((link) => (
                <IssueReferencePill key={link.id} issue={link.issue} />
              ))}
            </div>
          </CasePropertyRow>
        )}
      </PropertySection>

      <PropertySection title={`Children${childCases.length > 0 ? ` ${childCases.length}` : ""}`}>
        <CaseChildrenTree cases={childCases} />
      </PropertySection>

      {caseData.attachments.length > 0 ? (
        <PropertySection title="Attachments">
          <CasePropertyRow label="Files" mode={mode}>
            <span className="text-caption text-[var(--fg-mute)]">
              {caseData.attachments.length} {caseData.attachments.length === 1 ? "file" : "files"}
            </span>
          </CasePropertyRow>
        </PropertySection>
      ) : null}
    </div>
  );
}

export function CaseDetailPage({ caseId }: { caseId: string }) {
  const { read } = useTeamsApi();
  const caseQuery = useCaseDetail(caseId);
  const eventsQuery = useCaseEvents(caseId);
  const caseData = caseQuery.data;
  // Children come from the server-side parent filter. All statuses, so the tree
  // shows completed/cancelled children too.
  const childrenQuery = useCaseChildren(caseData?.id);
  const agentsQuery = read<{ agents: CompanyAgent[] }>("/agents");
  const { patchCase } = useCaseMutations();

  const [copied, setCopied] = useState(false);
  const [saving, setSaving] = useState(false);

  const backHref = caseHref();

  if (!caseData) {
    if (!caseQuery.error) return <PageSkeleton />;
    return (
      <div className="mx-auto max-w-md py-16 text-center">
        <p className="text-ui text-[var(--fg-mute)]">Case not found.</p>
        <Link
          href={backHref}
          prefetch={false}
          onClick={(e) => handleShellLinkClick(e, backHref)}
          className="mt-2 inline-block text-ui text-[var(--link)] hover:underline"
        >
          ← Back to cases
        </Link>
      </div>
    );
  }

  const childCases = childrenQuery.data?.cases ?? [];
  const events = eventsQuery.data?.events ?? [];
  const agents = agentsQuery.data?.agents ?? [];
  const documents = caseDocuments(caseData);
  const description = caseData.fields.description ?? caseData.fields.Description ?? null;

  async function save(input: { status?: CaseStatus; labelIds?: string[] }) {
    setSaving(true);
    try {
      await patchCase(caseId, input);
    } catch (e) {
      notifyApiError(e, "Couldn't update the case.");
    } finally {
      setSaving(false);
    }
  }

  function copyCaseToClipboard(currentCase: CaseDetail) {
    const markdown = [
      `# ${currentCase.identifier} ${currentCase.title}`,
      "",
      `- Key: ${currentCase.key ?? "none"}`,
      `- Type: ${currentCase.caseType}`,
      `- Status: ${STATUS_LABEL[currentCase.status]}`,
      currentCase.labels.length > 0 ? `- Labels: ${currentCase.labels.map((label) => label.name).join(", ")}` : "- Labels: none",
    ].join("\n");
    void navigator.clipboard.writeText(markdown).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }

  const properties = (mode: CasePropertyDisplayMode) => (
    <CasePropertiesContent
      caseData={caseData}
      childCases={childCases}
      labelsPending={saving}
      onLabelIdsChange={(labelIds) => void save({ labelIds })}
      mode={mode}
    />
  );

  return (
    <div className="grid grid-cols-1 gap-6 p-4 text-[var(--fg)] sm:p-6 @3xl:grid-cols-[minmax(0,1fr)_320px]">
      <main className="min-w-0 space-y-6">
        <nav className="flex items-center gap-1 text-caption text-[var(--fg-mute)]" aria-label="Breadcrumb">
          <Link
            href={backHref}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, backHref)}
            className="hover:underline"
          >
            Cases
          </Link>
          <ChevronRight size={12} strokeWidth={1.75} />
          <span className="truncate text-[var(--fg-soft)]">
            {caseData.identifier} — {caseData.title}
          </span>
        </nav>

        <header className="space-y-3">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0 space-y-1">
              <CaseIdentifierKey identifier={caseData.identifier} caseKey={caseData.key} />
              <h1 className="text-xl font-bold">{caseData.title}</h1>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <CaseStatusPicker
                status={caseData.status}
                disabled={saving}
                align="end"
                onChange={(status) => void save({ status })}
              />
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="h-6 w-6"
                    aria-label="More case actions"
                    title="More case actions"
                  >
                    <MoreVertical className="h-4 w-4" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-48 p-1">
                  <button
                    type="button"
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-caption hover:bg-[var(--plane-raised)]"
                    onClick={() => copyCaseToClipboard(caseData)}
                  >
                    {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                    Copy as markdown
                  </button>
                </PopoverContent>
              </Popover>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{caseData.caseType}</Badge>
          </div>

          <CaseRelationshipsSection parent={caseData.parent} childCases={childCases} />
        </header>

        <Tabs defaultValue="overview" className="space-y-4">
          <TabsList variant="line" className="w-full justify-start gap-1">
            <TabsTrigger value="overview" className="flex-none">Overview</TabsTrigger>
            <TabsTrigger value="properties" className="flex-none">Properties</TabsTrigger>
            <TabsTrigger value="activity" className="flex-none">
              Activity{events.length > 0 && <span className="ml-1 text-[var(--fg-mute)]">{events.length}</span>}
            </TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="space-y-6">
            {/* Upstream's detail page never shows `summary` (the case's one-line
                gist), so a case without documents left Overview empty. */}
            {caseData.summary ? (
              <div className="text-ui text-[var(--fg-soft)]">
                <MarkdownView>{caseData.summary}</MarkdownView>
              </div>
            ) : null}

            {documents.length > 0 ? (
              <section className="space-y-2">
                <h2 className="text-ui font-semibold">Documents</h2>
                <ul className="flex flex-col gap-2">
                  {documents.map((doc) => (
                    <DocumentRow
                      key={doc.id}
                      doc={doc}
                      agents={agents}
                      issueId={caseData.id}
                      basePath={`/cases/${caseData.id}`}
                      onRestored={() => void caseQuery.mutate()}
                    />
                  ))}
                </ul>
              </section>
            ) : null}

            {description ? (
              <section className="space-y-2">
                <h2 className="text-ui font-semibold">Description</h2>
                <div className="rounded-lg border border-[var(--edge)] px-4 py-3">
                  <CaseFieldValue value={description} />
                </div>
              </section>
            ) : null}

            {caseData.attachments.length > 0 && (
              <section className="space-y-2">
                <h2 className="text-ui font-semibold">Attachments ({caseData.attachments.length})</h2>
                <CaseAttachmentsGallery caseId={caseData.id} attachments={caseData.attachments} />
              </section>
            )}
          </TabsContent>

          <TabsContent value="properties">{properties("full")}</TabsContent>

          <TabsContent value="activity">
            <CaseActivityFeed events={events} />
          </TabsContent>
        </Tabs>
      </main>

      <aside className="@3xl:sticky @3xl:top-4 @3xl:self-start">{properties("compact")}</aside>
    </div>
  );
}
