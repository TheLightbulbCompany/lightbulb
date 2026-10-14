# Pause, reading-only and deletion notice

Rules (founder decisions A12, A13):

1. **One 30-day pause per company**, offered only when a lapse is about timing. Admin dashboard → user → Billing actions → **Pause 30 days**. A trial moves its end 30 days out (from today if it already ended); a live Stripe subscription has collection paused for 30 days (`pause_collection.behavior=void`). The billing row's `paused_at` makes a second pause refuse with "already used its one 30-day pause".
2. **Reading-only.** A trial that ends without a plan, or a cancelled subscription, stops agent work and keeps everything else: messages, files, board, memory, agent profiles, and people messaging each other.
3. **Deletion notice.** 90 days after an unpicked trial ended, the trial sweep (`core/services/trial_lifecycle.py`) emails the owner once (`deletion_notice_at`). Nothing is deleted automatically; deletion is a separate, human decision after the notice.
4. **Day-27 mail.** The same sweep sends one recap email from the CEO agent's address 3-4 days before the trial ends (`trial_recap_emailed_at`).
5. **Refunds.** No refund policy beyond ordinary billing disputes: an unexpected or duplicate charge is refunded through the normal Stripe dispute route.

To cancel at a customer's request: Stripe Dashboard → the subscription (metadata `workspace_id`) → Cancel at period end. `customer.subscription.deleted` tears the runtime down and keeps profiles; check the workspace shows reading-only.
