# 09 — Horizontal analogues: "who may do what, on whose behalf, with what limits, and who approved" outside AI agents

Date: 2026-11-02. Tags: [P] = I fetched the primary page and the claim is quoted or closely paraphrased from it. [S] = secondary, a search summary, or my own inference; treat as unverified.
Fetch budget used: about 42 of 45. Pages fetched are listed at the end. Reddit was not used.

---

## 1. Summary findings (12)

1. EU payment law already requires an agent-safe statement of who agreed to what: under SCA dynamic linking, the authentication code must be "specific to the amount of the payment transaction and the payee", and "any change to the amount or the payee results in the invalidation of the authentication code". [P] https://www.legislation.gov.uk/eur/2018/389/article/5/adopted
2. Regulators allow skipping approval only on narrow, recorded grounds: a trusted-beneficiary list (set up under SCA) or a transaction-risk score under a fraud-rate and amount threshold. That is "thresholds plus pre-approved counterparties", not "trust the actor". [P] https://www.legislation.gov.uk/eur/2018/389/article/13/adopted , https://www.legislation.gov.uk/eur/2018/389/article/18/adopted
3. Google's AP2 is the closest existing design to Lightbulb's signed request: the user signs an *open* mandate holding constraints (allowed payee, amount range, budget, recurrence, execution date, bound to one agent key), the agent signs the *closed* mandate, and verifiers "always receive a closed Payment and Checkout Mandate". [P] https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/payment_mandate.md , .../specification.md
4. AP2 states the mandate-plus-receipt chain exists so disputes can be settled: together they give "a non-repudiable picture of the transaction" and "evidence as to what the user, and each role saw". AP2 covers payments only. Its own authors say the model "could be applied more generally in the future". [P] https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/agent_authorization.md
5. Card networks already separate "the person was present" from "acting on an earlier instruction": an MIT "relates to a previous consumer-initiated transaction" and is "conducted without the consumer present". That is the human-present / human-not-present split that agents need. [P] https://developer.visa.com/capabilities/card-on-file-data-inquiry/docs-getting-started
6. Army doctrine says how to write intent so subordinates can decide without asking: intent is "purpose... key tasks, and conditions that define the end state", it "nests within the commander's intent two levels up", and mission orders "do not contain directives... that dictate how a task is to be accomplished". [P] ADP 6-0 (July 2026) https://armypubs.army.mil/epubs/DR_pubs/DR_a/ARN47032-ADP_6-0-000-WEB-1.pdf
7. DORA found that heavyweight external change approval does not lower failure rates and harms delivery. It recommends peer review recorded in the platform to satisfy segregation of duties, plus automation. CAB-style queues are what agents will drown in. [P] https://dora.dev/capabilities/streamlining-change-approval/
8. ServiceNow writes "pre-approved" down as an artifact: standard changes are "pre-authorized", skip "group level or peer approval and CAB authorization", and live in "a catalog of templates". Emergency changes skip peer review and go straight to authorization. [P] https://www.servicenow.com/docs/r/xanadu/it-service-management/change-management/change-types.html
9. JIT access is sold today as time-boxed, reasoned, multi-approver requests: Teleport requires a non-empty reason when configured, caps `max_duration` at 14 days, uses approval thresholds (default 1), and blocks self-approval. Pricing is per monthly active user, and "submitting an access request" counts as activity. [P] https://goteleport.com/docs/identity-governance/access-requests/access-request-configuration/ , https://goteleport.com/api/files/teleport-pricing-guide.pdf
10. Every serious system keeps a break-glass path and records each use of it: Microsoft says alert on every emergency-account sign-in (threshold >0, severity 0 "Critical"), run a post-mortem on each use, and drill at least every 90 days. [P] https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/security-emergency-access
11. The audit buyer exists because of statute: SOX 404 requires management to assess internal control over financial reporting and the auditor to attest to it. Smaller filers are exempt from the attestation. SoD tools (SailPoint) sell "be aware of violations, but allow exceptions", with mitigating controls that expire. [P] https://www.law.cornell.edu/uscode/text/15/7262 , https://documentation.sailpoint.com/saas/help/sod/index.html
12. The delegation primitives exist but almost none record *who decided*: UCAN binds operands (policy "MUST constrain the `args`") and attenuates per hop, Biscuit attenuates offline with Datalog checks, macaroons attenuate with caveats, and AWS `sourceIdentity` survives role chaining and "cannot be changed". None of them carries an escalation or approver record. Okta for AI Agents (GA Oct 2026) sells registration, connection rules, a kill switch and logs, but the page I read does not mention per-request operand binding. [P] https://github.com/ucan-wg/delegation , https://doc.biscuitsec.org/reference/specifications , https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_monitor.html , https://www.okta.com/newsroom/press-releases/ai-innovations-oktane-2026/

---

## 2. Table (domains × a–f)

| Domain | (a) Artifact, who writes it | (b) Limits | (c) Outside the rules | (d) Record, who reads | (e) Pay / trigger | (f) Agent version; sold? |
|---|---|---|---|---|---|---|
| 1 Finance DoA / approvals | Approval policy in the spend tool (Ramp), written by admins; changing SoD toggle is "business owners" only [P] | Amount, user role (all plans); merchant, category, department, entity, accounting code (Plus) [P] | Fallback approver when the spender is in the chain (SoD) [P]; escalation not documented [P-absent] | Tool history; auditors (SOX) [S] | Ramp Plus tier gates the richer conditions [P]; trigger is scale and audit [S] | Agent spend under the same policy. Ramp/Brex agent cards exist [S, not fetched] |
| 2 SoD / IGA | SoD policy = two conflicting access lists (SailPoint) [P] | Conflicting entitlements; mitigating control with an expiry [P/S] | Allowed violation plus a mitigating control with an expiration [S from search of SailPoint docs] | Violation reports, audit events for policy changes [P]; SOX 404 auditor [P] | Okta IGA is an unpriced add-on; Okta base $6–17/user/mo, $1,500 minimum [P]; trigger SOX [P] | SoD across agent chains (an agent must not both request and approve). Okta for AI Agents: certifications, no SoD-for-agents claim seen [P] |
| 3 PAM / JIT | Role with `allow.request` rules (Teleport), written by security [P] | Roles, resources, `max_duration` ≤14 days, approval threshold N, reason required [P] | Break-glass accounts outside CA policy, alert on every use, post-mortem [P] | Session recordings plus structured audit logs "for every session and request" [P]; security, auditors | Teleport per MAU/TPR, Identity Governance a separate meter, prices not public [P] | Agent JIT grant per task with reason and expiry. Teleport/Okta move toward agent identity [P/S] |
| 4 Change mgmt | Change model / standard-change template catalog (ServiceNow), owned by Change Mgmt [P] | Risk class: standard / normal / emergency [P] | Emergency change bypasses peer review, goes to Authorize [P] | Change record; CAB; auditors [P/S] | ServiceNow ITSM (price not fetched) [S]; DORA: external approval does not cut failures [P] | Agent actions classed as "standard" (pre-approved template) vs "normal" (route to owner). Not seen sold [S] |
| 5 Payments / mandates | Mandate/consent signed by the payer; AP2 mandate signed on a trusted surface [P] | Amount + payee (SCA), payee set, amount range, budget, recurrence, execution date, instrument (AP2) [P] | SCA step-up; exemptions only via trusted beneficiaries or TRA under thresholds [P] | Mandates + receipts as dispute evidence [P]; networks, issuers | Mandated by PSD2 [P]; card networks compete on agentic tokens [S] | Already exists: AP2, Visa Trusted Agent Protocol [P], Mastercard Agent Pay (blocked) |
| 6 Legal agency | Board resolution / constitution / POA; bank signing mandates [S] | Scope of authority in the instrument [S] | Third party in good faith not bound by internal limits (UK CA 2006 s.40) [P]; apparent authority binds principal [P] | Courts read the record [S] | Lawyers, banks; trigger is a transaction [S] | Agent's published authority card so counterparties can rely on it. Not sold as such [S] |
| 7 Capability systems | Token/tuple/policy written by the resource owner or service [P] | Caveats (macaroons), Datalog checks (Biscuit), `args` policy (UCAN), conditions on `sourceIdentity` (AWS) [P] | Revocation as "last resort" (UCAN) [P]; no escalation primitive in any [P-absent] | CloudTrail records `sourceIdentity` [P]; UCAN chain "is by definition a provenance log" [P] | Zanzibar inside Google at trillions of ACLs [P]; AWS IAM is free and bundled [S] | Signed per-hop request that binds operands and the decision. Pieces exist, assembled product not seen [S] |
| 8 Military | Commander's intent + mission orders, written by the commander personally [P] | "Minimum constraints", nested intent two levels up [P] | Disciplined initiative: act when orders no longer fit, "reporting to the commander... when able" [P] | Reports up the chain [P] | n/a | Intent document an agent reasons against, with a duty to report. No product [S] |

---

## 3. Per-domain detail

### 1. Delegation-of-authority matrices and approval workflows (finance, procurement)
- Ramp expense approval policies [P] https://support.ramp.com/hc/en-us/articles/27132114712723-Setting-up-expense-approval-policies-for-transactions-and-reimbursements
  - Routing: "amount and user role-based routing is available to all customers". Ramp Plus adds business entity, direct manager, employee, merchant, merchant category, department, location and accounting codes.
  - Approvers: any admin, manager and manager's manager, department/location owner, custom groups, specific employees. Multi-approver steps are "require all" or "require any". There are also notify-only steps.
  - SoD: a toggle reroutes approval to a fallback approver when the spender is in the chain. Only business owners can change it.
  - Auto-approve: transactions set to auto-approve "skip team reviewer queues but remain visible". Threshold-based auto-approval rules are not described on this page.
  - Not supported: workflows cannot be exported or duplicated, and changes do not apply retroactively.
- The Ramp Bill Pay approvals article (from search, not fetched): amount routing for all, other conditions on Plus [S] https://support.ramp.com/hc/en-us/articles/4417843897747-Bill-Pay-approvals
- Coupa, NetSuite, SAP, Brex: not fetched (budget). See Not verified.
- Read for Lightbulb: the DoA artifact in practice is a routing table keyed on amount × role × counterparty × cost centre. Its premium tier is "more condition fields", which suggests buyers pay for richer conditions.

### 2. Segregation of duties and access governance
- SailPoint SoD [P] https://documentation.sailpoint.com/saas/help/sod/index.html : "Human identities in your org with access in one list aren't allowed to have access within the other list." The goal is to "Identify which violations need to be corrected and which can be allowed." Audit events cover policy create, update and delete. Note the word "Human": the SoD policy text is scoped to human identities.
- From the SailPoint violation-reports doc (search excerpt) [S]: mitigating controls carry an "Expiration date or relative duration". The API has `POST /violations/v1/{id}/controls`.
- SOX 404 [P] https://www.law.cornell.edu/uscode/text/15/7262 : management states its responsibility "for establishing and maintaining an adequate internal control structure" and assesses effectiveness. The auditor attests (404(b)). Non-accelerated filers are exempt from (b), and so are EGCs.
- Okta pricing [P] https://www.okta.com/pricing/ : Starter $6, Core Essentials $14, Essentials $17 per user/month, annual, with a $1,500 minimum. Identity Governance is an add-on with no published price. Privileged Access is licensed by Resource Units.
- Read: exceptions are first-class records with an owner and an expiry. The buying trigger is the auditor.

### 3. PAM and just-in-time access
- Teleport access requests [P] https://goteleport.com/docs/identity-governance/access-requests/ : approval by "a configurable number of approvers", and "users cannot approve their own requests". There are automatic reviews.
- Teleport configuration [P] https://goteleport.com/docs/identity-governance/access-requests/access-request-configuration/ : `reason.mode: required` → "The user must provide a non-empty reason". `max_duration` "can never exceed fourteen days". `thresholds.approve` defaults to 1, and there is no deny threshold. `suggested_reviewers` are merged across roles.
- Teleport pricing guide (rev. 16 June 2026) [P] https://goteleport.com/api/files/teleport-pricing-guide.pdf : usage-based on MAUs and Protected Resources. Active means any audit-log activity, "submitting an access request and so on". Identity Governance includes "Access requests and access lists with periodic access reviews". Product features listed: "Session recordings and structured audit logs for every session and request".
- Break-glass, Microsoft Entra [P] https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/security-emergency-access : "two or more emergency access accounts". Exclude them from blocking Conditional Access. "Configure alerts for every use". "After any use... conduct a review to determine whether the use was authorized". Validate "At least every 90 days". It also names a lock-out failure: approval-gated roles with "no approvers are selected" mean "no one can approve activation and tenant administration is effectively locked". That is the failure an escalation-to-authority-holder design must avoid.
- CyberArk, BeyondTrust, Delinea: not fetched.

### 4. Change management
- ServiceNow change types [P] https://www.servicenow.com/docs/r/xanadu/it-service-management/change-management/change-types.html : standard = "a pre-authorized change that is low risk, relatively common and follows a specified procedure"; skips "group level or peer approval and CAB authorization"; "predefined in a catalog of templates". Normal = two approval levels, including CAB. Emergency "bypasses group and peer review and approval" and goes to Authorize.
- DORA [P] https://dora.dev/capabilities/streamlining-change-approval/ : "no evidence was found to support the hypothesis that a more formal, external review process" was associated with lower change fail rates. Heavyweight approvals "have a negative impact on software delivery performance". Replace them with peer review enforced at check-in and recorded in the platform (this meets SoD), plus automated testing and monitoring. Keep the CAB for coordination. The page itself has no numbers.
- Read: the template catalog is the pre-approval artifact. Risk class decides the route. Evidence says approval by distant people adds delay without adding safety.

### 5. Payments and mandates
- SCA RTS Art. 5 dynamic linking [P] (UK copy of EU 2018/389 as adopted) https://www.legislation.gov.uk/eur/2018/389/article/5/adopted : "(a) the payer is made aware of the amount... and of the payee; (b) the authentication code generated is specific to the amount... and the payee...; (d) any change to the amount or the payee results in the invalidation of the authentication code generated." Integrity is required "throughout all of the phases of the authentication".
- Art. 13 trusted beneficiaries [P]: SCA is required to create or amend the list, after which payments to listed payees may skip SCA.
- Art. 18 transaction risk analysis [P]: an exemption only when the fraud rate is ≤ the reference rate, the amount ≤ the exemption threshold value, and real-time analysis finds no abnormal spending, device, malware, known fraud scenario or location.
- AP2 [P] (repo docs, fetched via GitHub API):
  - payment_mandate.md: constraints = Agent Recurrence, Allowed Payee, Allowed Payment Instrument, Allowed PISP, Amount Range, Budget ("requested amount plus the total sum of amounts from previously closed Payment Mandates MUST be less than or equal to `max`"), Reference, Execution Date. Closed `mandate.payment.1` vs open `mandate.payment.open.1`.
  - agent_authorization.md: two steps, "Mandate Delegation" and "Action Authorization". The verifier returns a Receipt. Open mandates are "bound to a particular Agent" and "MUST support cryptographic Key Binding". "even well-behaving Agents need to have their behavior tightly constrained above what a normal authorization model would require of human users."
  - specification.md: Human Present (user approves the closed checkout) vs Human Not Present (user approves "a set of constraints"). Dispute verification steps use hashes linking receipts to mandates. Retention and dispute procedure are "outside the scope".
- Visa [P] https://developer.visa.com/capabilities/card-on-file-data-inquiry/docs-getting-started : CIT = "consumer is present and provides their payment credentials". MIT "relates to a previous consumer-initiated transaction... without the consumer present", on instructions the consumer gave.
- Visa Trusted Agent Protocol [P] https://developer.visa.com/capabilities/trusted-agent-protocol/overview : "a verifiable, cryptographic standard to help merchants differentiate legitimate agents from nefarious bots". Uses RFC 9421 message signatures. The page does not list which fields are bound.
- Mastercard Agent Pay: 403, not read. SEPA mandate (EPC rulebook) and NACHA authorization rules: not read (NACHA URL 404).

### 6. Legal agency
- UK Companies Act 2006 s.40 [P] https://www.legislation.gov.uk/ukpga/2006/46/section/40 : "In favour of a person dealing with a company in good faith, the power of the directors to bind the company, or authorise others to do so, is deemed to be free of any limitation under the company's constitution." The counterparty "is not bound to enquire as to any limitation".
- Apparent authority, LII Wex [P] https://www.law.cornell.edu/wex/apparent_authority : exists if "a third party reasonably infers, from the principal's conduct, that the principal granted such power to the agent". Cites *ASME v. Hydrolevel*, 456 U.S. 566 (1982): "principals are liable when their agents act with apparent authority". Undisclosed internal limits do not remove apparent authority.
- Read: in law, internal limits do not protect the company against a good-faith counterparty. The protection has to be enforced *before* the act, at the company's own checkpoint, or published to the counterparty. That supports a company-controlled decision point, plus a signed statement the counterparty can check.
- Restatement (Third) of Agency text, POA scope statutes and bank mandate forms: not fetched.

### 7. Capability and delegation systems
- Macaroons [P] https://research.google/pubs/macaroons-cookies-with-contextual-caveats-for-decentralized-authorization-in-the-cloud/ : "embed caveats that attenuate and contextually confine when, where, by who" and "support decentralized delegation between principals".
- Biscuit [P] https://doc.biscuitsec.org/reference/specifications : blocks append checks, and the holder "cannot remove existing blocks without invalidating the signature". Attenuation is offline. The authorizer supplies request facts (resource, operation, time) and checks constrain them. Third-party blocks let an external party add a signed constraint or claim.
- UCAN [P] https://github.com/ucan-wg/spec , https://github.com/ucan-wg/delegation : capability = `subject x command x policy`. The policy "MUST constrain the `args` field of an eventual Invocation", so operands are bound. "The delegation chain is by definition a provenance log." Delegates can sub-delegate "without alerting their delegator", and revocation is "a last resort".
- Zanzibar [P] https://www.usenix.org/conference/atc19/presentation/pang : "trillions of access control lists and millions of authorization requests per second", p95 < 10 ms, used by Calendar, Cloud, Drive, Maps, Photos, YouTube. It is an ACL/relationship store and carries no operands.
- AWS source identity [P] https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_monitor.html : it "persists when a role is used to assume another role... role chaining" and "cannot be changed during the role session". It is recorded in CloudTrail `userIdentity.sessionContext.sourceIdentity`. Caveat: it is "not captured by CloudTrail when an AWS service or service-linked role carries out an action on behalf of" a federated identity. Session tags and conditions on them give attribute-based limits.
- Summary of the primitives: operands are bound by UCAN (args policy) and Biscuit (request facts). Per-hop attenuation: macaroons, Biscuit, UCAN. Original human preserved across hops: AWS sourceIdentity, UCAN chain. Who *decided*, with escalation: none. Commercial adoption: Zanzibar (Google-internal; OpenFGA/SpiceDB are open-source derivatives [S]); AWS IAM (universal). Macaroons, Biscuit and UCAN are niche [S].
- SPKI/SDSI, OPA/Gatekeeper and Kubernetes admission: not fetched.

### 8. Military
- ADP 6-0 Command and Control, July 2026 (supersedes the 31 July 2019 edition) [P] https://armypubs.army.mil/epubs/DR_pubs/DR_a/ARN47032-ADP_6-0-000-WEB-1.pdf
  - 1-64: intent is "a clear and concise expression of the purpose of an operation and the desired objectives and military end state". It "nests within the commander's intent two levels up".
  - 1-65/1-66: "Commanders develop their intent statement personally". It must be understood "two echelons lower". It includes "purpose, key tasks, and conditions that define the end state".
  - 1-73: mission orders "do not contain directives to a subordinate that dictate how a task is to be accomplished".
  - 1-77: disciplined initiative means following orders "until they realize that their orders and the plan are no longer suitable", then acting to achieve intent, "reporting to the commander about the new situation when able".
  - 3-54: "impose minimum constraints on subordinates".
- Rules of engagement, the two-person rule and positive control: not fetched. The standing ROE are partly classified.

---

## 4. Patterns that recur across domains

1. Thresholds plus pre-approved lists, never blanket trust: SCA (trusted beneficiaries, TRA under an amount), Ramp (amount routing), ServiceNow (standard-change catalog).
2. The approval binds the operands: SCA binds amount + payee, AP2 binds payee/amount/budget/date, UCAN binds args. Change the operand and the approval is void.
3. Open vs closed authority: a standing instruction with constraints (AP2 open mandate, card MIT, standard-change template, commander's intent), turned into a concrete act by the subordinate. The verifier always sees the concrete act.
4. The original principal persists across hops: AWS sourceIdentity is immutable through role chaining, UCAN proofs form a chain, AP2 presents the whole mandate chain.
5. Exceptions are records with an owner and an expiry: SailPoint mitigating controls, Teleport max_duration, Entra break-glass post-mortems.
6. Break-glass always exists and is always loud: alert on every use, review each use, drill every 90 days.
7. No self-approval and N-of-M approvers: Teleport, Ramp SoD fallback, DORA peer review satisfying SoD.
8. Approval by distant people adds delay without adding safety (DORA). Route to the people who own the resource or the change.
9. The record exists for a named reader: auditor (SOX 404), dispute (AP2 receipts), security (CloudTrail, session recording).
10. Internal limits do not bind good-faith outsiders (s.40, apparent authority), so enforcement has to sit at a company-controlled point before the act.
11. Intent is written short, by the top person, nested two levels, and states purpose and end state rather than method (ADP 6-0).
12. Pricing follows the governed actor: per user/MAU (Okta, Teleport). Premium tiers sell more condition fields (Ramp Plus).

## 5. What this says the first product should be

1. The artifact: a signed **Request Record** (the "closed mandate" for any cross-person agent action). It holds the requester, the agent chain (immutable origin like sourceIdentity), on whose behalf, operation, operands, the rule or approver that decided, and a decision receipt hash. Reuse the AP2 open/closed mandate shape and UCAN-style `args` policy rather than inventing one.
2. The policy artifact: a **standard-action catalog** (ServiceNow's template idea) plus an **authority map** (resource → authority holder, like department/vendor owners in Ramp). Out-of-catalog actions route to the holder with N-of-M, no self-approval, reason required and an expiry (Teleport fields).
3. A break-glass path from day one: allowed, loud, reviewed.
4. First wedge: the class of action where an auditor or a counterparty already reads the record. Candidates are agent-initiated spend/vendor actions and agent JIT access to production or customer data. Don't start with generic "intent".
5. Buyer: the controller/CFO or head of security/IT at a company already paying for an approval or IGA tool, who now has agents acting for staff. Trigger: the SOX/SOC 2 auditor asks "who approved this agent's action". Price per governed agent or MAU, matching how Okta and Teleport price.
6. What to avoid: a CAB-like queue to the agent owner. DORA evidence and the approval-fatigue pattern both argue for auto-allow inside the catalog and escalation only outside it.
7. Competition to watch: Okta for AI Agents (registration, A2A connection rules, kill switch, logs; GA Oct 2026). The page I read showed no per-request operand binding or escalation to the authority holder. AP2/Visa cover payments only. The open space is the non-payment, cross-person, operand-bound decision record.

## 6. Not verified

- Coupa, NetSuite, SAP, Brex approval workflows and prices. Any vendor "approval fatigue" features beyond Ramp auto-approve visibility.
- SailPoint and Saviynt pricing. SailPoint exception-recording details (only a search excerpt of the violation-reports page).
- CyberArk, BeyondTrust, Delinea JIT flows and pricing. Teleport's actual dollar prices (not published) and its audit-event names for access reviews.
- ServiceNow pricing. Risk-based auto-approval in ServiceNow (community posts only). DORA's 2019 quantitative figures.
- Mastercard Agent Pay (403). Visa Intelligent Commerce controls (no primary page found). Exact fields Visa TAP signs. SEPA mandate fields (EPC rulebook). NACHA authorization requirements (URL 404). How networks use mandates in chargebacks.
- Restatement (Third) of Agency text. POA statutes. How companies publish bank signing mandates. UK CA 2006 s.44 (fetch failed).
- SPKI/SDSI, OPA/Gatekeeper, Kubernetes admission controllers. Commercial adoption of OpenFGA/SpiceDB.
- Rules of engagement, the two-person rule, positive control (no primary doctrine fetched).
- Okta for AI Agents pricing and whether Agent Gateway binds request operands.
- Whether Ramp/Brex sell agent-specific cards or controls.

## Pages fetched (primary)

1. https://dora.dev/capabilities/streamlining-change-approval/
2. https://www.legislation.gov.uk/eur/2018/389/article/5/adopted
3. https://www.legislation.gov.uk/eur/2018/389/article/13/adopted
4. https://www.legislation.gov.uk/eur/2018/389/article/18/adopted
5. https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/agent_authorization.md
6. https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/payment_mandate.md
7. https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/checkout_mandate.md
8. https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/specification.md
9. https://goteleport.com/docs/identity-governance/access-requests/
10. https://goteleport.com/docs/identity-governance/access-requests/access-request-configuration/
11. https://goteleport.com/api/files/teleport-pricing-guide.pdf
12. https://developer.visa.com/capabilities/trusted-agent-protocol/overview
13. https://developer.visa.com/capabilities/card-on-file-data-inquiry/docs-getting-started
14. https://armypubs.army.mil/epubs/DR_pubs/DR_a/ARN47032-ADP_6-0-000-WEB-1.pdf
15. https://www.servicenow.com/docs/r/xanadu/it-service-management/change-management/change-types.html
16. https://documentation.sailpoint.com/saas/help/sod/index.html
17. https://www.okta.com/pricing/
18. https://support.ramp.com/hc/en-us/articles/27132114712723-Setting-up-expense-approval-policies-for-transactions-and-reimbursements
19. https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/security-emergency-access
20. https://www.legislation.gov.uk/ukpga/2006/46/section/40
21. https://www.law.cornell.edu/wex/apparent_authority
22. https://www.law.cornell.edu/uscode/text/15/7262
23. https://research.google/pubs/macaroons-cookies-with-contextual-caveats-for-decentralized-authorization-in-the-cloud/
24. https://github.com/ucan-wg/spec (README)
25. https://github.com/ucan-wg/delegation (README)
26. https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_monitor.html
27. https://www.usenix.org/conference/atc19/presentation/pang
28. https://doc.biscuitsec.org/reference/specifications
29. https://www.okta.com/newsroom/press-releases/ai-innovations-oktane-2026/

Failed or blocked: eur-lex (AWS WAF), ap2-protocol.org/specification (404), mastercard.com Agent Pay (403), nacha.org rule page (404), docs.ramp.com (JS shell), support.ramp.com approval-policies index, armypubs 2019 ADP 6-0 (removed), JP 5-0 (403).
