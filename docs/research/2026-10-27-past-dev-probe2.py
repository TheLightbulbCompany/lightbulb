import json, os, sys, time, urllib.request, urllib.error
KEY=os.environ["PAST_API_KEY"]; B="https://api.past.dev/api/v1"; OUT=sys.argv[1]
def call(m,p,b=None):
    r=urllib.request.Request(B+p,method=m,data=None if b is None else json.dumps(b).encode(),headers={"Authorization":"Bearer "+KEY,"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r,timeout=60) as x: return x.status,json.loads(x.read() or b"null")
    except urllib.error.HTTPError as e: return e.code,json.loads(e.read() or b"null")
def session(project,branch,turns):
    head=f"# Claude Code session — {project} ({branch})\n\n"
    return head+"\n\n".join(f"## {who} · {at}\n{text}" for who,at,text in turns)+"\n"
def cc(i,project,branch,ts,turns,ident="dev-sam"):
    return {"id":f"claude-code:probe2-{i}","label":f"Claude Code · {project} · {branch}","identity":ident,"timestamp":ts,
            "content":session(project,branch,turns),"metadata":{"source":"claude-code","project":project,"gitBranch":branch}}
items=[
 # A. real plugin-format coding sittings
 cc("s1","shop-api","fix/webhooks","2026-07-03T13:02:00Z",[
  ("Developer","2026-07-03T13:02:00Z","Stripe webhooks are failing locally again, every event comes back 400 signature verification failed. Prod is fine."),
  ("Claude","2026-07-03T13:03:10Z","I checked the handler in src/billing/webhooks.ts. It reads STRIPE_WEBHOOK_SECRET for every environment. The Stripe CLI forwarding locally signs with the test-mode endpoint secret, which is different from the live one in your .env. That mismatch produces exactly this 400."),
  ("Developer","2026-07-03T13:06:40Z","ok that's it. make it read STRIPE_WEBHOOK_SECRET_TEST when NODE_ENV is development and keep the live one otherwise."),
  ("Claude","2026-07-03T13:09:05Z","Done. webhooks.ts now picks the secret by NODE_ENV, and I added a startup warning when the test secret is missing. Local events verify now. Worth remembering: when signatures fail only locally, check which mode's secret is loaded before touching the verification code."),
 ]),
 cc("s2","shop-api","feat/pricing-cache","2026-07-10T09:15:00Z",[
  ("Developer","2026-07-10T09:15:00Z","Should we cache product prices in Redis? The pricing endpoint is slow."),
  ("Claude","2026-07-10T09:16:30Z","Prices change hourly from the supplier feed, and a stale price shown at checkout is a refund. I'd rather add an index on price_rules(product_id) and measure first."),
  ("Developer","2026-07-10T09:20:00Z","Agreed, no Redis cache for prices. Add the index."),
  ("Claude","2026-07-10T09:31:00Z","Index added in migration 0042. p95 on /pricing went from 820ms to 140ms locally. Decision recorded: no price caching in Redis."),
 ]),
 cc("s3","shop-api","fix/webhook-dupes","2026-07-20T16:40:00Z",[
  ("Developer","2026-07-20T16:40:00Z","Customers got charged twice for some orders yesterday. Looks like webhook events were processed twice."),
  ("Claude","2026-07-20T16:44:00Z","The HTTP client in src/lib/http.ts retries on timeout, and Stripe also retries, so the same event ran twice. I moved webhook processing onto the job queue with the event id as the idempotency key and turned off the client's retries for this route."),
  ("Developer","2026-07-20T16:50:00Z","Great. So from now on webhooks go through the queue, never straight from the HTTP handler."),
 ]),
 # B. clean contradictions, no authority cue, no reply-to
 {"id":"probe2-launch-ana","label":"#launch","identity":"ana","timestamp":"2026-08-04T10:00:00Z","content":"The public launch is on March 3."},
 {"id":"probe2-launch-ben","label":"#marketing","identity":"ben","timestamp":"2026-08-04T15:00:00Z","content":"The public launch is on March 10."},
 {"id":"probe2-docker-1","label":"#eng","identity":"dev-sam","timestamp":"2026-08-05T10:00:00Z","content":"I containerized the API with Docker last week and it runs in compose now."},
 {"id":"probe2-docker-2","label":"#eng","identity":"dev-sam","timestamp":"2026-08-12T10:00:00Z","content":"I've never used Docker on this project; everything still runs directly on the host."},
 # C. clean rephrased updates
 {"id":"probe2-pg-1","label":"#infra","identity":"dev-sam","timestamp":"2026-09-01T10:00:00Z","content":"We run Postgres 14 in production."},
 {"id":"probe2-pg-2","label":"#infra","identity":"dev-sam","timestamp":"2026-09-15T08:00:00Z","content":"Finished the database upgrade last night, production is on 16 now."},
 {"id":"probe2-vac","label":"#infra","identity":"dev-sam","timestamp":"2026-09-20T08:00:00Z","content":"Can you help me tune autovacuum for Postgres 16 on the orders table?"},
 {"id":"probe2-lat-1","label":"#perf","identity":"dev-sam","timestamp":"2026-09-02T10:00:00Z","content":"API p95 latency is 420ms."},
 {"id":"probe2-lat-2","label":"#perf","identity":"dev-sam","timestamp":"2026-09-25T10:00:00Z","content":"After the connection pooling change, response time at the 95th percentile dropped to 300ms."},
 {"id":"probe2-mil-1","label":"#search","identity":"dev-sam","timestamp":"2026-09-03T10:00:00Z","content":"I'm evaluating Milvus 2.2.0 for indexing our product catalogue."},
 {"id":"probe2-mil-2","label":"#search","identity":"dev-sam","timestamp":"2026-09-28T10:00:00Z","content":"I need help with the Milvus 2.3.1 setup for the product catalogue cluster."},
]
log={"ingest":call("POST","/ingest/batch",{"idempotencyKey":"lb-probe-2","items":items})}
iid=log["ingest"][1].get("ingestionId"); t0=time.time()
while True:
    st=call("GET",f"/ingest/{iid}")[1]
    if st.get("status") in ("completed","failed") and (st.get("settled") or st.get("blocked")): break
    if time.time()-t0>900: break
    time.sleep(10)
log["settle_seconds"]=round(time.time()-t0); log["explain"]=call("GET",f"/ingest/{iid}/explain")[1]
log["lineage"]={}; log["nodes"]={}
for it in log["explain"].get("items",[]):
    lin=call("GET",f"/trace/lineage/{it['sourceId']}?revealAll=true&limit=80")[1]; log["lineage"][it["dataPointId"]]=lin
    for n in lin.get("nodes",[]):
        if n.get("kind")=="memory" and n["id"] not in log["nodes"]: log["nodes"][n["id"]]=call("GET",f"/trace/nodes/{n['id']}")[1]
Q=[("wh_sig","stripe webhook signature verification failing in dev again, can you look at it?",10),
   ("redis","should we cache prices in redis to speed up the pricing page?",10),
   ("wh_now","how are stripe webhooks processed now?",10),
   ("launch","When is the public launch?",10),
   ("docker","Does the API run in Docker?",10),
   ("pg","What Postgres version is production on?",10),
   ("lat","What is the API p95 latency?",10),
   ("milvus","Which Milvus version are we using for the catalogue?",10)]
log["recall"]={n:call("POST","/recall",{"query":q,"identity":"dev-sam","limit":l})[1] for n,q,l in Q}
log["usage"]=call("GET","/usage")[1]["balance"]
json.dump(log,open(OUT,"w"),indent=1,ensure_ascii=False); print("settled",log["settle_seconds"],"s; memories",len(log["nodes"]),"; credits left",log["usage"]["left"])
