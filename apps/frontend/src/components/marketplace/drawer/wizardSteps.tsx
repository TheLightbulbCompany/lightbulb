"use client";

import type { PublishForm } from "./PublishWizard";

export function PickAgentStep({
  selected,
  onSelect,
}: {
  selected?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <div>
      Pick an agent (placeholder, Task 13).
      <button onClick={() => onSelect("agent_pulse")}>Pick Pulse</button>
      {selected && <span>selected={selected}</span>}
    </div>
  );
}

export function DetailsStep({
  form,
  onChange: _onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  return (
    <div>
      Details (placeholder, Task 13). name=&quot;{form.name}&quot;
    </div>
  );
}

export function PricingStep({
  form,
  onChange: _onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  return (
    <div>
      Pricing (placeholder, Task 14). tier={form.priceTier}
    </div>
  );
}

export function ReviewStep({ form }: { form: PublishForm }) {
  return (
    <div>
      Review (placeholder, Task 14). agent={form.agentId}
    </div>
  );
}

export function SuccessStep({ onViewMine }: { onViewMine: () => void }) {
  return (
    <div className="text-center py-8">
      <div className="text-3xl mb-3">✓</div>
      <h3 className="font-editorial-serif text-2xl mb-2">Submitted for review.</h3>
      <p className="text-[14px] text-[#5b5b5b] mb-4">
        You&apos;ll get a notification when it&apos;s live — usually within 24 hours.
      </p>
      <button
        onClick={onViewMine}
        className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full font-semibold"
      >
        View my listings →
      </button>
    </div>
  );
}
