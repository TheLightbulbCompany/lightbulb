import json, os, sys, time, urllib.request, urllib.error
KEY=os.environ["PAST_API_KEY"]; B="https://api.past.dev/api/v1"
OUT=sys.argv[1]
def call(method, path, body=None):
    req=urllib.request.Request(B+path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization":"Bearer "+KEY,"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"null")
log={}
# identities and audiences
for ident,traits in [("fin-ana",{"team":"finance"}),("eng-ben",{"team":"engineering"}),("pm-kai",{"team":"product"})]:
    call("PUT",f"/identities/{ident}",{"traits":traits})
log["aud_fin"]=call("PUT","/audiences/finance",{"kind":"fixed","name":"Finance","identities":["fin-ana"]})
log["aud_eng"]=call("PUT","/audiences/eng",{"kind":"rule","name":"Engineering","rule":{"match":"all","conditions":[{"trait":"team","operator":"is","values":["engineering"]}]}})
P="probe-"
items=[
 # 1 contradiction / update over time
 {"id":P+"budget-1","label":"Call notes, Acme kickoff","identity":"nadia","timestamp":"2026-01-10T15:00:00Z","content":"Kickoff call with Acme. Nadia said the pilot budget is 30k for Q1."},
 {"id":P+"budget-2","label":"Email from Nadia","identity":"nadia","timestamp":"2026-02-03T09:00:00Z","content":"Quick update: Acme's finance team approved more. The pilot budget moved to 40k."},
 # 2 ownership handover
 {"id":P+"owner-1","label":"#eng standup","identity":"priya","timestamp":"2026-03-01T10:00:00Z","content":"Priya: I own the billing migration and will have the schema done by mid March."},
 {"id":P+"owner-2","label":"#eng standup","identity":"tomas","timestamp":"2026-03-20T10:00:00Z","content":"Tomás: I've taken over the billing migration from Priya. She moved to onboarding this week."},
 # 3 backdated fact
 {"id":P+"globex","label":"Sales recap","identity":"kai","timestamp":"2026-04-15T12:00:00Z","content":"Recap: back on March 3 we signed Globex for 12 seats. Renewal is due next March."},
 # 4 events sent out of order (ingest order: launch, kickoff, beta)
 {"id":P+"evt-launch","label":"Launch note","identity":"kai","timestamp":"2026-05-20T16:00:00Z","content":"Atlas launched to all customers today."},
 {"id":P+"evt-kickoff","label":"Project kickoff","identity":"kai","timestamp":"2026-04-28T09:00:00Z","content":"Kicked off project Atlas with the design team."},
 {"id":P+"evt-beta","label":"Beta note","identity":"kai","timestamp":"2026-05-05T11:00:00Z","content":"Atlas beta opened to 20 design partners."},
 # 5 contested, unresolved
 {"id":P+"deadline-ana","label":"DM Ana","identity":"ana","timestamp":"2026-06-02T09:00:00Z","content":"Ana: the Zephyr contract deadline is June 30."},
 {"id":P+"deadline-ben","label":"DM Ben","identity":"ben","timestamp":"2026-06-02T09:30:00Z","content":"Ben: I don't think that's right, the Zephyr deadline is July 15 according to legal."},
 # 6 rule / convention
 {"id":P+"rule-deploy","label":"#eng handbook","identity":"tomas","timestamp":"2026-06-10T08:00:00Z","content":"Team rule: we deploy on Tuesdays and Thursdays only, never on Fridays."},
 # 7 retraction
 {"id":P+"offsite-1","label":"#general","identity":"kai","timestamp":"2026-06-12T08:00:00Z","content":"The September offsite will be in Lisbon."},
 {"id":P+"offsite-2","label":"#general","identity":"kai","timestamp":"2026-06-14T08:00:00Z","content":"Correction to my earlier message: the offsite is not in Lisbon. It will be in Porto."},
 # 8 audiences
 {"id":P+"payroll","label":"Finance close","identity":"fin-ana","audience":"finance","timestamp":"2026-07-01T09:00:00Z","content":"Q2 payroll total came to 410k."},
 {"id":P+"dbrotate","label":"Eng ops","identity":"eng-ben","audience":"eng","timestamp":"2026-07-02T09:00:00Z","content":"The production database credentials rotate every Friday at 18:00 UTC."},
 # 9 coding-session style lesson
 {"id":P+"lesson","label":"Claude Code session, repo lightbulb, branch fix/webhooks","identity":"eng-ben","timestamp":"2026-07-03T14:00:00Z","content":"Session summary: Stripe webhooks failed locally because the signing secret differs between test and live mode. Fix: read STRIPE_WEBHOOK_SECRET_TEST when NODE_ENV is development. Next time check the mode before debugging signatures."},
]
log["ingest"]=call("POST","/ingest/batch",{"idempotencyKey":"lb-probe-1","items":items})
iid=log["ingest"][1].get("ingestionId")
t0=time.time()
while True:
    st=call("GET",f"/ingest/{iid}")[1]
    if st.get("status") in ("completed","failed") and st.get("settled") is not None and (st.get("settled") or st.get("blocked")): break
    if time.time()-t0>900: break
    time.sleep(10)
log["settle_seconds"]=round(time.time()-t0); log["status"]=st
log["explain"]=call("GET",f"/ingest/{iid}/explain")[1]
# lineage per source
log["lineage"]={}
for it in (log["explain"] or {}).get("items",[]):
    sid=it.get("sourceId")
    if sid: log["lineage"][it["dataPointId"]]=call("GET",f"/trace/lineage/{sid}?revealAll=true&limit=60")[1]
# node details for every memory (current, validity, replaced)
log["nodes"]={}
for dp,lin in log["lineage"].items():
    for n in (lin or {}).get("nodes",[]):
        if n.get("kind")=="memory" and n["id"] not in log["nodes"]:
            log["nodes"][n["id"]]=call("GET",f"/trace/nodes/{n['id']}")[1]
Q=[
 ("budget_now","What is the Acme pilot budget?","pm-kai",None,None),
 ("budget_jan20","What is the Acme pilot budget?","pm-kai","2026-01-20T00:00:00Z",None),
 ("owner_now","Who owns the billing migration?","pm-kai",None,None),
 ("owner_mar10","Who owns the billing migration?","pm-kai","2026-03-10T00:00:00Z",None),
 ("globex","When did we sign Globex and for how many seats?","pm-kai",None,None),
 ("globex_mar10","Have we signed Globex?","pm-kai","2026-03-10T00:00:00Z",None),
 ("atlas_order","What happened on project Atlas, in order?","pm-kai",None,"chronological"),
 ("deadline","What is the Zephyr contract deadline?","pm-kai",None,None),
 ("deploy","Can I deploy on Friday?","pm-kai",None,None),
 ("offsite","Where is the September offsite?","pm-kai",None,None),
 ("payroll_fin","What was Q2 payroll?","fin-ana",None,None),
 ("payroll_eng","What was Q2 payroll?","eng-ben",None,None),
 ("dbrotate_eng","When do production database credentials rotate?","eng-ben",None,None),
 ("dbrotate_fin","When do production database credentials rotate?","fin-ana",None,None),
 ("lesson","Stripe webhook signature verification fails in development","eng-ben",None,None),
]
log["recall"]={}
for name,q,ident,ts,sort in Q:
    b={"query":q,"identity":ident,"limit":8}
    if ts: b["queryTimestamp"]=ts
    if sort: b["sort"]=sort
    log["recall"][name]=call("POST","/recall",b)[1]
log["usage"]=call("GET","/usage")[1]
json.dump(log,open(OUT,"w"),indent=1,ensure_ascii=False)
print("settled in",log["settle_seconds"],"s; status",st.get("status"),"; memories",len(log["nodes"]))
