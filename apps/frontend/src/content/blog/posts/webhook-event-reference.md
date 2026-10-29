---
title: "Webhook event reference: 480 verified triggers across 10 providers"
description: "A source-backed reference for Lightbulb webhook triggers: 480 verified event types across 10 inbound-trigger providers, plus the five connected providers that do not currently expose inbound trigger blocks."
date: "2026-08-20"
author: "Prasiddha Parthsarthy"
tags: ["webhooks","integrations","automation","providers"]
---

> Verified from `apps/backend/core/services/credentials/providers.yaml` on 2026-08-20. The registry currently exposes **480 event types across 10 inbound-trigger providers**. Five connected providers are included below as non-trigger integrations because they do not have inbound trigger blocks in the registry.

## What changed

Lightbulb agents can now subscribe to a registry-backed set of webhook triggers instead of hand-rolled one-off callbacks. This reference is generated from the same provider registry that powers connection setup, so the public list and product wiring stay aligned.

## Provider coverage

| Provider | Registry key | Inbound trigger events | Registration mode | Signing mode |
| --- | --- | ---: | --- | --- |
| Stripe | `stripe` | 265 | `guided_paste` | `hmac_sha256` |
| Vercel | `vercel_cli` | 98 | `auto_vercel` | `hmac_sha1` |
| GitHub | `github` | 53 | `auto_github` | `github_hmac` |
| Attio | `attio` | 27 | `guided_paste` | `github_hmac` |
| Linear | `linear` | 15 | `guided_paste` | `github_hmac` |
| Sentry | `sentry` | 8 | `guided_paste` | `github_hmac` |
| Google Workspace | `google` | 4 | `auto_google_watch` | `bearer` |
| PostHog | `posthog` | 4 | `guided_paste` | `bearer` |
| Meta Ads | `meta_ads` | 3 | `auto_meta` | `bearer` |
| Instagram | `instagram` | 3 | `auto_meta` | `bearer` |

These 10 providers make up the 480-event claim. Stripe contributes 265 of them; Vercel contributes 98; GitHub contributes 53.

## Connected providers without inbound trigger blocks

| Provider | Registry key | Trigger status |
| --- | --- | --- |
| Notion | `notion` | No `inbound` block in the registry |
| Intercom | `intercom` | No `inbound` block in the registry |
| Parallel (Web Search) | `parallel` | No `inbound` block in the registry |
| X | `x` | No `inbound` block in the registry |
| AWS | `aws` | No `inbound` block in the registry |

Notion, Intercom, Parallel, X, and AWS are still connected-provider entries, but this reference does **not** claim inbound trigger behavior for them.

## Event reference

### Stripe (`stripe`) — 265 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `invoice.payment_failed` | Invoice payment failed | — |
| `charge.refunded` | Charge refunded | — |
| `customer.subscription.deleted` | Subscription canceled | — |
| `account.application.authorized` | Account application authorized | — |
| `account.application.deauthorized` | Account application deauthorized | — |
| `account.external_account.created` | Account external account created | — |
| `account.external_account.deleted` | Account external account deleted | — |
| `account.external_account.updated` | Account external account updated | — |
| `account.updated` | Account updated | — |
| `application_fee.created` | Application fee created | — |
| `application_fee.refund.updated` | Application fee refund updated | — |
| `application_fee.refunded` | Application fee refunded | — |
| `balance.available` | Balance available | — |
| `balance_settings.updated` | Balance settings updated | — |
| `billing.alert.triggered` | Billing alert triggered | — |
| `billing.credit_balance_transaction.created` | Billing credit balance transaction created | — |
| `billing.credit_grant.created` | Billing credit grant created | — |
| `billing.credit_grant.updated` | Billing credit grant updated | — |
| `billing.meter.created` | Billing meter created | — |
| `billing.meter.deactivated` | Billing meter deactivated | — |
| `billing.meter.reactivated` | Billing meter reactivated | — |
| `billing.meter.updated` | Billing meter updated | — |
| `billing_portal.configuration.created` | Billing portal configuration created | — |
| `billing_portal.configuration.updated` | Billing portal configuration updated | — |
| `billing_portal.session.created` | Billing portal session created | — |
| `capability.updated` | Capability updated | — |
| `cash_balance.funds_available` | Cash balance funds available | — |
| `charge.captured` | Charge captured | — |
| `charge.dispute.closed` | Charge dispute closed | — |
| `charge.dispute.created` | Charge dispute created | — |
| `charge.dispute.funds_reinstated` | Charge dispute funds reinstated | — |
| `charge.dispute.funds_withdrawn` | Charge dispute funds withdrawn | — |
| `charge.dispute.updated` | Charge dispute updated | — |
| `charge.expired` | Charge expired | — |
| `charge.failed` | Charge failed | — |
| `charge.pending` | Charge pending | — |
| `charge.refund.updated` | Charge refund updated | — |
| `charge.succeeded` | Charge succeeded | — |
| `charge.updated` | Charge updated | — |
| `checkout.session.async_payment_failed` | Checkout session async payment failed | — |
| `checkout.session.async_payment_succeeded` | Checkout session async payment succeeded | — |
| `checkout.session.completed` | Checkout session completed | — |
| `checkout.session.expired` | Checkout session expired | — |
| `climate.order.canceled` | Climate order canceled | — |
| `climate.order.created` | Climate order created | — |
| `climate.order.delayed` | Climate order delayed | — |
| `climate.order.delivered` | Climate order delivered | — |
| `climate.order.product_substituted` | Climate order product substituted | — |
| `climate.product.created` | Climate product created | — |
| `climate.product.pricing_updated` | Climate product pricing updated | — |
| `coupon.created` | Coupon created | — |
| `coupon.deleted` | Coupon deleted | — |
| `coupon.updated` | Coupon updated | — |
| `credit_note.created` | Credit note created | — |
| `credit_note.updated` | Credit note updated | — |
| `credit_note.voided` | Credit note voided | — |
| `customer.created` | Customer created | — |
| `customer.deleted` | Customer deleted | — |
| `customer.discount.created` | Customer discount created | — |
| `customer.discount.deleted` | Customer discount deleted | — |
| `customer.discount.updated` | Customer discount updated | — |
| `customer.source.created` | Customer source created | — |
| `customer.source.deleted` | Customer source deleted | — |
| `customer.source.expiring` | Customer source expiring | — |
| `customer.source.updated` | Customer source updated | — |
| `customer.subscription.created` | Customer subscription created | — |
| `customer.subscription.paused` | Customer subscription paused | — |
| `customer.subscription.pending_update_applied` | Customer subscription pending update applied | — |
| `customer.subscription.pending_update_expired` | Customer subscription pending update expired | — |
| `customer.subscription.resumed` | Customer subscription resumed | — |
| `customer.subscription.trial_will_end` | Customer subscription trial will end | — |
| `customer.subscription.updated` | Customer subscription updated | — |
| `customer.tax_id.created` | Customer tax id created | — |
| `customer.tax_id.deleted` | Customer tax id deleted | — |
| `customer.tax_id.updated` | Customer tax id updated | — |
| `customer.updated` | Customer updated | — |
| `customer_cash_balance_transaction.created` | Customer cash balance transaction created | — |
| `entitlements.active_entitlement_summary.updated` | Entitlements active entitlement summary updated | — |
| `file.created` | File created | — |
| `financial_connections.account.account_numbers_updated` | Financial connections account account numbers updated | — |
| `financial_connections.account.created` | Financial connections account created | — |
| `financial_connections.account.deactivated` | Financial connections account deactivated | — |
| `financial_connections.account.disconnected` | Financial connections account disconnected | — |
| `financial_connections.account.expected_deactivation_date_updated` | Financial connections account expected deactivation date updated | — |
| `financial_connections.account.reactivated` | Financial connections account reactivated | — |
| `financial_connections.account.refreshed_balance` | Financial connections account refreshed balance | — |
| `financial_connections.account.refreshed_ownership` | Financial connections account refreshed ownership | — |
| `financial_connections.account.refreshed_transactions` | Financial connections account refreshed transactions | — |
| `financial_connections.account.supported_payment_method_types_updated` | Financial connections account supported payment method types updated | — |
| `financial_connections.account.upcoming_account_number_expiry` | Financial connections account upcoming account number expiry | — |
| `financial_connections.account.upcoming_deactivation` | Financial connections account upcoming deactivation | — |
| `financial_connections.authorization.expected_deactivation_date_updated` | Financial connections authorization expected deactivation date updated | — |
| `financial_connections.authorization.upcoming_deactivation` | Financial connections authorization upcoming deactivation | — |
| `identity.verification_session.canceled` | Identity verification session canceled | — |
| `identity.verification_session.created` | Identity verification session created | — |
| `identity.verification_session.processing` | Identity verification session processing | — |
| `identity.verification_session.redacted` | Identity verification session redacted | — |
| `identity.verification_session.requires_input` | Identity verification session requires input | — |
| `identity.verification_session.verified` | Identity verification session verified | — |
| `invoice.created` | Invoice created | — |
| `invoice.deleted` | Invoice deleted | — |
| `invoice.finalization_failed` | Invoice finalization failed | — |
| `invoice.finalized` | Invoice finalized | — |
| `invoice.marked_uncollectible` | Invoice marked uncollectible | — |
| `invoice.overdue` | Invoice overdue | — |
| `invoice.overpaid` | Invoice overpaid | — |
| `invoice.paid` | Invoice paid | — |
| `invoice.payment_action_required` | Invoice payment action required | — |
| `invoice.payment_attempt_required` | Invoice payment attempt required | — |
| `invoice.payment_succeeded` | Invoice payment succeeded | — |
| `invoice.sent` | Invoice sent | — |
| `invoice.upcoming` | Invoice upcoming | — |
| `invoice.updated` | Invoice updated | — |
| `invoice.voided` | Invoice voided | — |
| `invoice.will_be_due` | Invoice will be due | — |
| `invoice_payment.paid` | Invoice payment paid | — |
| `invoiceitem.created` | Invoice item created | — |
| `invoiceitem.deleted` | Invoice item deleted | — |
| `issuing_authorization.created` | Issuing authorization created | — |
| `issuing_authorization.request` | Issuing authorization request | — |
| `issuing_authorization.updated` | Issuing authorization updated | — |
| `issuing_card.created` | Issuing card created | — |
| `issuing_card.updated` | Issuing card updated | — |
| `issuing_cardholder.created` | Issuing cardholder created | — |
| `issuing_cardholder.updated` | Issuing cardholder updated | — |
| `issuing_dispute.closed` | Issuing dispute closed | — |
| `issuing_dispute.created` | Issuing dispute created | — |
| `issuing_dispute.funds_reinstated` | Issuing dispute funds reinstated | — |
| `issuing_dispute.funds_rescinded` | Issuing dispute funds rescinded | — |
| `issuing_dispute.submitted` | Issuing dispute submitted | — |
| `issuing_dispute.updated` | Issuing dispute updated | — |
| `issuing_personalization_design.activated` | Issuing personalization design activated | — |
| `issuing_personalization_design.deactivated` | Issuing personalization design deactivated | — |
| `issuing_personalization_design.rejected` | Issuing personalization design rejected | — |
| `issuing_personalization_design.updated` | Issuing personalization design updated | — |
| `issuing_token.created` | Issuing token created | — |
| `issuing_token.updated` | Issuing token updated | — |
| `issuing_transaction.created` | Issuing transaction created | — |
| `issuing_transaction.purchase_details_receipt_updated` | Issuing transaction purchase details receipt updated | — |
| `issuing_transaction.updated` | Issuing transaction updated | — |
| `mandate.updated` | Mandate updated | — |
| `payment_intent.amount_capturable_updated` | Payment intent amount capturable updated | — |
| `payment_intent.canceled` | Payment intent canceled | — |
| `payment_intent.created` | Payment intent created | — |
| `payment_intent.partially_funded` | Payment intent partially funded | — |
| `payment_intent.payment_failed` | Payment intent payment failed | — |
| `payment_intent.processing` | Payment intent processing | — |
| `payment_intent.requires_action` | Payment intent requires action | — |
| `payment_intent.succeeded` | Payment intent succeeded | — |
| `payment_link.created` | Payment link created | — |
| `payment_link.updated` | Payment link updated | — |
| `payment_method.attached` | Payment method attached | — |
| `payment_method.automatically_updated` | Payment method automatically updated | — |
| `payment_method.detached` | Payment method detached | — |
| `payment_method.updated` | Payment method updated | — |
| `payout.canceled` | Payout canceled | — |
| `payout.created` | Payout created | — |
| `payout.failed` | Payout failed | — |
| `payout.paid` | Payout paid | — |
| `payout.reconciliation_completed` | Payout reconciliation completed | — |
| `payout.updated` | Payout updated | — |
| `person.created` | Person created | — |
| `person.deleted` | Person deleted | — |
| `person.updated` | Person updated | — |
| `plan.created` | Plan created | — |
| `plan.deleted` | Plan deleted | — |
| `plan.updated` | Plan updated | — |
| `price.created` | Price created | — |
| `price.deleted` | Price deleted | — |
| `price.updated` | Price updated | — |
| `product.created` | Product created | — |
| `product.deleted` | Product deleted | — |
| `product.updated` | Product updated | — |
| `promotion_code.created` | Promotion code created | — |
| `promotion_code.updated` | Promotion code updated | — |
| `quote.accepted` | Quote accepted | — |
| `quote.canceled` | Quote canceled | — |
| `quote.created` | Quote created | — |
| `quote.finalized` | Quote finalized | — |
| `radar.early_fraud_warning.created` | Radar early fraud warning created | — |
| `radar.early_fraud_warning.updated` | Radar early fraud warning updated | — |
| `refund.created` | Refund created | — |
| `refund.failed` | Refund failed | — |
| `refund.updated` | Refund updated | — |
| `reporting.report_run.failed` | Reporting report run failed | — |
| `reporting.report_run.succeeded` | Reporting report run succeeded | — |
| `reporting.report_type.updated` | Reporting report type updated | — |
| `reserve.hold.created` | Reserve hold created | — |
| `reserve.hold.updated` | Reserve hold updated | — |
| `reserve.plan.created` | Reserve plan created | — |
| `reserve.plan.disabled` | Reserve plan disabled | — |
| `reserve.plan.expired` | Reserve plan expired | — |
| `reserve.plan.updated` | Reserve plan updated | — |
| `reserve.release.created` | Reserve release created | — |
| `review.closed` | Review closed | — |
| `review.opened` | Review opened | — |
| `setup_intent.canceled` | Setup intent canceled | — |
| `setup_intent.created` | Setup intent created | — |
| `setup_intent.requires_action` | Setup intent requires action | — |
| `setup_intent.setup_failed` | Setup intent setup failed | — |
| `setup_intent.succeeded` | Setup intent succeeded | — |
| `sigma.scheduled_query_run.created` | Sigma scheduled query run created | — |
| `source.canceled` | Source canceled | — |
| `source.chargeable` | Source chargeable | — |
| `source.failed` | Source failed | — |
| `source.mandate_notification` | Source mandate notification | — |
| `source.refund_attributes_required` | Source refund attributes required | — |
| `source.transaction.created` | Source transaction created | — |
| `source.transaction.updated` | Source transaction updated | — |
| `subscription_schedule.aborted` | Subscription schedule aborted | — |
| `subscription_schedule.canceled` | Subscription schedule canceled | — |
| `subscription_schedule.completed` | Subscription schedule completed | — |
| `subscription_schedule.created` | Subscription schedule created | — |
| `subscription_schedule.expiring` | Subscription schedule expiring | — |
| `subscription_schedule.released` | Subscription schedule released | — |
| `subscription_schedule.updated` | Subscription schedule updated | — |
| `tax.settings.updated` | Tax settings updated | — |
| `tax_rate.created` | Tax rate created | — |
| `tax_rate.updated` | Tax rate updated | — |
| `terminal.reader.action_failed` | Terminal reader action failed | — |
| `terminal.reader.action_succeeded` | Terminal reader action succeeded | — |
| `terminal.reader.action_updated` | Terminal reader action updated | — |
| `test_helpers.test_clock.advancing` | Test helpers test clock advancing | — |
| `test_helpers.test_clock.created` | Test helpers test clock created | — |
| `test_helpers.test_clock.deleted` | Test helpers test clock deleted | — |
| `test_helpers.test_clock.internal_failure` | Test helpers test clock internal failure | — |
| `test_helpers.test_clock.ready` | Test helpers test clock ready | — |
| `topup.canceled` | Topup canceled | — |
| `topup.created` | Topup created | — |
| `topup.failed` | Topup failed | — |
| `topup.reversed` | Topup reversed | — |
| `topup.succeeded` | Topup succeeded | — |
| `transfer.created` | Transfer created | — |
| `transfer.reversed` | Transfer reversed | — |
| `transfer.updated` | Transfer updated | — |
| `treasury.credit_reversal.created` | Treasury credit reversal created | — |
| `treasury.credit_reversal.posted` | Treasury credit reversal posted | — |
| `treasury.debit_reversal.completed` | Treasury debit reversal completed | — |
| `treasury.debit_reversal.created` | Treasury debit reversal created | — |
| `treasury.debit_reversal.initial_credit_granted` | Treasury debit reversal initial credit granted | — |
| `treasury.financial_account.closed` | Treasury financial account closed | — |
| `treasury.financial_account.created` | Treasury financial account created | — |
| `treasury.financial_account.features_status_updated` | Treasury financial account features status updated | — |
| `treasury.inbound_transfer.canceled` | Treasury inbound transfer canceled | — |
| `treasury.inbound_transfer.created` | Treasury inbound transfer created | — |
| `treasury.inbound_transfer.failed` | Treasury inbound transfer failed | — |
| `treasury.inbound_transfer.succeeded` | Treasury inbound transfer succeeded | — |
| `treasury.outbound_payment.canceled` | Treasury outbound payment canceled | — |
| `treasury.outbound_payment.created` | Treasury outbound payment created | — |
| `treasury.outbound_payment.expected_arrival_date_updated` | Treasury outbound payment expected arrival date updated | — |
| `treasury.outbound_payment.failed` | Treasury outbound payment failed | — |
| `treasury.outbound_payment.posted` | Treasury outbound payment posted | — |
| `treasury.outbound_payment.returned` | Treasury outbound payment returned | — |
| `treasury.outbound_payment.tracking_details_updated` | Treasury outbound payment tracking details updated | — |
| `treasury.outbound_transfer.canceled` | Treasury outbound transfer canceled | — |
| `treasury.outbound_transfer.created` | Treasury outbound transfer created | — |
| `treasury.outbound_transfer.expected_arrival_date_updated` | Treasury outbound transfer expected arrival date updated | — |
| `treasury.outbound_transfer.failed` | Treasury outbound transfer failed | — |
| `treasury.outbound_transfer.posted` | Treasury outbound transfer posted | — |
| `treasury.outbound_transfer.returned` | Treasury outbound transfer returned | — |
| `treasury.outbound_transfer.tracking_details_updated` | Treasury outbound transfer tracking details updated | — |
| `treasury.received_credit.created` | Treasury received credit created | — |
| `treasury.received_credit.failed` | Treasury received credit failed | — |
| `treasury.received_credit.succeeded` | Treasury received credit succeeded | — |
| `treasury.received_debit.created` | Treasury received debit created | — |

### Vercel (`vercel_cli`) — 98 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `deployment.error` | Deployment failed | `deployment.error` |
| `deployment.succeeded` | Deployment succeeded | `deployment.succeeded` |
| `deployment.created` | Deployment started | `deployment.created` |
| `ai-gateway.auto-reload.limit-reached` | AI Gateway auto-reload limit reached | `ai-gateway.auto-reload.limit-reached` |
| `ai-gateway.balance-depleted` | AI Gateway balance depleted | `ai-gateway.balance-depleted` |
| `alerts.triggered` | Alerts triggered | `alerts.triggered` |
| `botid.anomaly` | Botid anomaly | `botid.anomaly` |
| `budget.reached` | Budget reached | `budget.reached` |
| `comment.created` | Comment created | `comment.created` |
| `comment.deleted` | Comment deleted | `comment.deleted` |
| `comment.mentioned` | Comment mentioned | `comment.mentioned` |
| `comment.reaction-added` | Comment reaction added | `comment.reaction-added` |
| `comment.reaction-removed` | Comment reaction removed | `comment.reaction-removed` |
| `comment.resolved` | Comment resolved | `comment.resolved` |
| `comment.unresolved` | Comment unresolved | `comment.unresolved` |
| `comment.updated` | Comment updated | `comment.updated` |
| `deployment.blocked` | Deployment blocked | `deployment.blocked` |
| `deployment.build-requested` | Deployment build requested | `deployment.build-requested` |
| `deployment.canceled` | Deployment canceled | `deployment.canceled` |
| `deployment.check-rerequested` | Deployment check rerequested | `deployment.check-rerequested` |
| `deployment.checkrun.cancel` | Deployment checkrun cancel | `deployment.checkrun.cancel` |
| `deployment.checkrun.start` | Deployment checkrun start | `deployment.checkrun.start` |
| `deployment.checks.failed` | Deployment checks failed | `deployment.checks.failed` |
| `deployment.checks.succeeded` | Deployment checks succeeded | `deployment.checks.succeeded` |
| `deployment.cleanup` | Deployment cleanup | `deployment.cleanup` |
| `deployment.integration.action.cancel` | Deployment integration action cancel | `deployment.integration.action.cancel` |
| `deployment.integration.action.cleanup` | Deployment integration action cleanup | `deployment.integration.action.cleanup` |
| `deployment.integration.action.start` | Deployment integration action start | `deployment.integration.action.start` |
| `deployment.promoted` | Deployment promoted | `deployment.promoted` |
| `deployment.ready` | Deployment ready | `deployment.ready` |
| `deployment.rollback` | Deployment rollback | `deployment.rollback` |
| `domain.auto-renew.changed` | Domain auto renew changed | `domain.auto-renew.changed` |
| `domain.certificate.add` | Domain certificate add | `domain.certificate.add` |
| `domain.certificate.add.failed` | Domain certificate add failed | `domain.certificate.add.failed` |
| `domain.certificate.deleted` | Domain certificate deleted | `domain.certificate.deleted` |
| `domain.certificate.renew` | Domain certificate renew | `domain.certificate.renew` |
| `domain.certificate.renew.failed` | Domain certificate renew failed | `domain.certificate.renew.failed` |
| `domain.created` | Domain created | `domain.created` |
| `domain.dns.records.changed` | Domain dns records changed | `domain.dns.records.changed` |
| `domain.renewal` | Domain renewal | `domain.renewal` |
| `domain.renewal.failed` | Domain renewal failed | `domain.renewal.failed` |
| `domain.transfer-in.completed` | Domain transfer in completed | `domain.transfer-in.completed` |
| `domain.transfer-in.failed` | Domain transfer in failed | `domain.transfer-in.failed` |
| `domain.transfer-in.started` | Domain transfer in started | `domain.transfer-in.started` |
| `edge-config.created` | Edge config created | `edge-config.created` |
| `edge-config.deleted` | Edge config deleted | `edge-config.deleted` |
| `edge-config.items.updated` | Edge config items updated | `edge-config.items.updated` |
| `firewall.attack` | Firewall attack | `firewall.attack` |
| `firewall.custom-rule-anomaly` | Firewall custom rule anomaly | `firewall.custom-rule-anomaly` |
| `firewall.system-rule-anomaly` | Firewall system rule anomaly | `firewall.system-rule-anomaly` |
| `flag.created` | Flag created | `flag.created` |
| `flag.deleted` | Flag deleted | `flag.deleted` |
| `flag.segment.created` | Flag segment created | `flag.segment.created` |
| `flag.segment.deleted` | Flag segment deleted | `flag.segment.deleted` |
| `flag.segment.updated` | Flag segment updated | `flag.segment.updated` |
| `flag.updated` | Flag updated | `flag.updated` |
| `function.archival-required` | Function archival required | `function.archival-required` |
| `function.removal-required` | Function removal required | `function.removal-required` |
| `integration-configuration.permission-upgraded` | Integration configuration permission upgraded | `integration-configuration.permission-upgraded` |
| `integration-configuration.removed` | Integration configuration removed | `integration-configuration.removed` |
| `integration-configuration.scope-change-confirmed` | Integration configuration scope change confirmed | `integration-configuration.scope-change-confirmed` |
| `integration-configuration.transferred` | Integration configuration transferred | `integration-configuration.transferred` |
| `integration-resource.project-connected` | Integration resource project connected | `integration-resource.project-connected` |
| `integration-resource.project-disconnected` | Integration resource project disconnected | `integration-resource.project-disconnected` |
| `marketplace.invoice.created` | Marketplace invoice created | `marketplace.invoice.created` |
| `marketplace.invoice.notpaid` | Marketplace invoice notpaid | `marketplace.invoice.notpaid` |
| `marketplace.invoice.overdue` | Marketplace invoice overdue | `marketplace.invoice.overdue` |
| `marketplace.invoice.paid` | Marketplace invoice paid | `marketplace.invoice.paid` |
| `marketplace.invoice.refunded` | Marketplace invoice refunded | `marketplace.invoice.refunded` |
| `marketplace.member.changed` | Marketplace member changed | `marketplace.member.changed` |
| `message.created` | Message created | `message.created` |
| `message.deleted` | Message deleted | `message.deleted` |
| `message.mentioned` | Message mentioned | `message.mentioned` |
| `message.reaction-added` | Message reaction added | `message.reaction-added` |
| `message.reaction-removed` | Message reaction removed | `message.reaction-removed` |
| `message.updated` | Message updated | `message.updated` |
| `observability.anomaly` | Observability anomaly | `observability.anomaly` |
| `observability.anomaly-error` | Observability anomaly error | `observability.anomaly-error` |
| `observability.error-anomaly` | Observability error anomaly | `observability.error-anomaly` |
| `observability.usage-anomaly` | Observability usage anomaly | `observability.usage-anomaly` |
| `project.created` | Project created | `project.created` |
| `project.domain.created` | Project domain created | `project.domain.created` |
| `project.domain.deleted` | Project domain deleted | `project.domain.deleted` |
| `project.domain.moved` | Project domain moved | `project.domain.moved` |
| `project.domain.unverified` | Project domain unverified | `project.domain.unverified` |
| `project.domain.updated` | Project domain updated | `project.domain.updated` |
| `project.domain.verified` | Project domain verified | `project.domain.verified` |
| `project.env-variable.created` | Project env variable created | `project.env-variable.created` |
| `project.env-variable.deleted` | Project env variable deleted | `project.env-variable.deleted` |
| `project.env-variable.updated` | Project env variable updated | `project.env-variable.updated` |
| `project.removed` | Project removed | `project.removed` |
| `project.renamed` | Project renamed | `project.renamed` |
| `project.rolling-release.aborted` | Project rolling release aborted | `project.rolling-release.aborted` |
| `project.rolling-release.approved` | Project rolling release approved | `project.rolling-release.approved` |
| `project.rolling-release.completed` | Project rolling release completed | `project.rolling-release.completed` |
| `project.rolling-release.started` | Project rolling release started | `project.rolling-release.started` |
| `thread.resolved` | Thread resolved | `thread.resolved` |
| `thread.unresolved` | Thread unresolved | `thread.unresolved` |

### GitHub (`github`) — 53 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `issues` | Issue opened or changed | `issues` |
| `pull_request` | Pull request opened or changed | `pull_request` |
| `push` | Code pushed | `push` |
| `branch_protection_configuration` | Branch protection configuration | `branch_protection_configuration` |
| `branch_protection_rule` | Branch protection rule | `branch_protection_rule` |
| `check_run` | Check run | `check_run` |
| `check_suite` | Check suite | `check_suite` |
| `code_scanning_alert` | Code scanning alert | `code_scanning_alert` |
| `commit_comment` | Commit comment | `commit_comment` |
| `create` | Branch or tag created | `create` |
| `custom_property_values` | Custom property values | `custom_property_values` |
| `delete` | Branch or tag deleted | `delete` |
| `dependabot_alert` | Dependabot alert | `dependabot_alert` |
| `deploy_key` | Deploy key | `deploy_key` |
| `deployment` | Deployment | `deployment` |
| `deployment_status` | Deployment status | `deployment_status` |
| `discussion` | Discussion | `discussion` |
| `discussion_comment` | Discussion comment | `discussion_comment` |
| `fork` | Fork | `fork` |
| `gollum` | Wiki page edited (gollum) | `gollum` |
| `issue_comment` | Issue comment | `issue_comment` |
| `issue_dependencies` | Issue dependencies | `issue_dependencies` |
| `label` | Label | `label` |
| `member` | Member | `member` |
| `meta` | Meta | `meta` |
| `milestone` | Milestone | `milestone` |
| `package` | Package | `package` |
| `page_build` | Page build | `page_build` |
| `project` | Project | `project` |
| `project_card` | Project card | `project_card` |
| `project_column` | Project column | `project_column` |
| `public` | Repository made public | `public` |
| `pull_request_review` | Pull request review | `pull_request_review` |
| `pull_request_review_comment` | Pull request review comment | `pull_request_review_comment` |
| `pull_request_review_thread` | Pull request review thread | `pull_request_review_thread` |
| `registry_package` | Registry package | `registry_package` |
| `release` | Release | `release` |
| `repository` | Repository | `repository` |
| `repository_advisory` | Repository advisory | `repository_advisory` |
| `repository_import` | Repository import | `repository_import` |
| `repository_ruleset` | Repository ruleset | `repository_ruleset` |
| `repository_vulnerability_alert` | Repository vulnerability alert | `repository_vulnerability_alert` |
| `secret_scanning_alert` | Secret scanning alert | `secret_scanning_alert` |
| `secret_scanning_alert_location` | Secret scanning alert location | `secret_scanning_alert_location` |
| `secret_scanning_scan` | Secret scanning scan | `secret_scanning_scan` |
| `security_and_analysis` | Security and analysis | `security_and_analysis` |
| `star` | Star | `star` |
| `status` | Status | `status` |
| `sub_issues` | Sub issues | `sub_issues` |
| `team_add` | Team add | `team_add` |
| `watch` | Watch | `watch` |
| `workflow_job` | Workflow job | `workflow_job` |
| `workflow_run` | Workflow run | `workflow_run` |

### Attio (`attio`) — 27 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `record.created` | Record created | — |
| `record.updated` | Record updated | — |
| `task.created` | Task created | — |
| `call-recording.created` | Call recording created | — |
| `comment.created` | Comment created | — |
| `comment.deleted` | Comment deleted | — |
| `comment.resolved` | Comment resolved | — |
| `comment.unresolved` | Comment unresolved | — |
| `list-attribute.created` | List attribute created | — |
| `list-attribute.updated` | List attribute updated | — |
| `list-entry.created` | List entry created | — |
| `list-entry.deleted` | List entry deleted | — |
| `list-entry.updated` | List entry updated | — |
| `list.created` | List created | — |
| `list.deleted` | List deleted | — |
| `list.updated` | List updated | — |
| `note-content.updated` | Note content updated | — |
| `note.created` | Note created | — |
| `note.deleted` | Note deleted | — |
| `note.updated` | Note updated | — |
| `object-attribute.created` | Object attribute created | — |
| `object-attribute.updated` | Object attribute updated | — |
| `record.deleted` | Record deleted | — |
| `record.merged` | Record merged | — |
| `task.deleted` | Task deleted | — |
| `task.updated` | Task updated | — |
| `workspace-member.created` | Workspace member created | — |

### Linear (`linear`) — 15 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `Issue` | Issue created or updated | — |
| `Comment` | New comment | — |
| `ProjectUpdate` | Project update posted | — |
| `Attachment` | Issue attachment | — |
| `Customer` | Customer | — |
| `CustomerNeed` | Customer request | — |
| `Cycle` | Cycle | — |
| `Document` | Document | — |
| `Initiative` | Initiative | — |
| `InitiativeUpdate` | Initiative update | — |
| `IssueLabel` | Issue label | — |
| `IssueSLA` | Issue SLA | — |
| `Project` | Project | — |
| `Reaction` | Comment reaction | — |
| `User` | User | — |

### Sentry (`sentry`) — 8 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `issue` | Issue created or changed | — |
| `event_alert` | Issue alert fired | — |
| `metric_alert` | Metric alert fired | — |
| `comment` | Comment | — |
| `error` | Error | — |
| `installation` | Installation | — |
| `preprod_artifact` | Preprod artifact | — |
| `seer` | Seer | — |

### Google Workspace (`google`) — 2 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `calendar.changed` | Calendar changed | — |
| `drive.changed` | Drive file changed | — |
| `gmail.poll` | New email (checked on a schedule) | — |
| `gmail.message` | New email (real-time push) | — |

### PostHog (`posthog`) — 4 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `$identify` | User identified | — |
| `$pageview` | Page viewed | — |
| `$exception` | Exception captured | — |
| `any` | Any event (choose in PostHog's filter) | — |

### Meta Ads (`meta_ads`) — 3 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `leadgen` | New lead | `leadgen` |
| `feed` | Page post activity | `feed` |
| `messages` | New message | `messages` |

### Instagram (`instagram`) — 3 events

| Event ID | Label | Provider event(s) |
| --- | --- | --- |
| `comments` | New comment | `comments` |
| `mentions` | Mentioned | `mentions` |
| `messages` | New DM | `messages` |

## Verification notes

- The total is the sum of `inbound.events` for the 10 provider registry entries above: 265 + 98 + 53 + 27 + 15 + 8 + 4 + 4 + 3 + 3 = **480**.
- The five non-trigger providers above are listed separately because their registry entries have no `inbound` block.
- The verified public claim is **480 events across 10 inbound-trigger providers**, plus five connected providers without inbound triggers.

