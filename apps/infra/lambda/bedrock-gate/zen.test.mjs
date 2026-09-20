import test from 'node:test';
import assert from 'node:assert/strict';
import { handleZen } from './zen.mjs';

function setup(billing = {subscription_status:{S:'active'},plan:{S:'entry'}}) {
  const out = {status:0, chunks:[]};
  const deps = {env:{MUSE_ENABLED:'true',ZEN_SECRET_ID:'fleet',TOKENS_TABLE:'tokens',BILLING_TABLE:'billing'},
    ddb:{getItem:async p => { assert.equal(p.ConsistentRead,true); return {Item:p.TableName === 'tokens' ? {owner_id:{S:'org_one'}} : billing}; }},
    getZenKey:async()=> 'fleet-secret', fetchImpl:async (url,init)=> {
      assert.equal(url,'https://opencode.ai/zen/v1/responses');
      assert.equal(init.headers.authorization,'Bearer fleet-secret');
      assert.equal(init.redirect,'error');
      assert.equal(JSON.parse(init.body).store,false);
      return new Response('data: {"ok":true}\n\n',{headers:{'content-type':'text/event-stream'}});
    }};
  const responder={headersSent:()=>!!out.status,writeHead:(s,h)=>Object.assign(out,{status:s,headers:h}),write:c=>out.chunks.push(Buffer.from(c)),end:()=>{},error:e=>Object.assign(out,{status:e.status,error:JSON.parse(e.body())})};
  const event={headers:{authorization:'Bearer owner-token','x-api-key':'never'},body:JSON.stringify({model:'muse-spark-1.3',input:'hello',stream:true})};
  return {out,deps,responder,event};
}
test('included Muse streams with zero credits and substitutes only the fleet credential',async()=>{
 const s=setup(); await handleZen(s.event,s.responder,s.deps); assert.equal(s.out.status,200); assert.match(Buffer.concat(s.out.chunks).toString(),/ok/);
});
test('unarmed, expired, inactive, missing and unselected accounts fail closed',async()=>{
 for(const billing of [undefined,{subscription_status:{S:'trial_local'}},{subscription_status:{S:'trial_local'},trial_end:{N:'1'}},{subscription_status:{S:'canceled'},plan:{S:'entry'}},{subscription_status:{S:'active'},plan:{S:'standard'}}]) {
 const s=setup(); s.deps.ddb.getItem=async p=>({Item:p.TableName==='tokens'?{owner_id:{S:'org_one'}}:billing}); s.deps.fetchImpl=()=>assert.fail('upstream called');
 await handleZen(s.event,s.responder,s.deps); assert.equal(s.out.status,403);
 }
});
test('wrong model, stateful references and invalid caps rejected; disabled fails closed',async()=>{
 for(const patch of [{model:'other'},{previous_response_id:'resp_other'},{conversation:'conv_other'},{max_output_tokens:0},{input:[{type:'item_reference',id:'other'}]}]) {
 const s=setup();s.event.body=JSON.stringify({model:'muse-spark-1.3',input:'hello',...patch}); await handleZen(s.event,s.responder,s.deps);assert.equal(s.out.status,400);
 }
 const s=setup();s.deps.env.MUSE_ENABLED='false';await handleZen(s.event,s.responder,s.deps);assert.equal(s.out.status,503);
});
test('revoked and unknown tokens cannot invoke',async()=>{
 for(const token of [undefined,{owner_id:{S:'org_one'},denied:{BOOL:true}}]) {const s=setup();s.deps.ddb.getItem=async()=>({Item:token});await handleZen(s.event,s.responder,s.deps);assert.ok([401,403].includes(s.out.status));}
});
test('upstream retry errors preserved',async()=>{
 const s=setup();s.deps.fetchImpl=async()=>new Response('{"error":{"message":"slow down"}}',{status:429,headers:{'retry-after':'7'}});await handleZen(s.event,s.responder,s.deps);assert.equal(s.out.status,429);assert.equal(s.out.headers['retry-after'],'7');
});
test('absolute trial deadline aborts an accepted request',async()=>{
 const s=setup({subscription_status:{S:'trial_local'},trial_end:{N:String((Date.now()+35)/1000)}});
 s.deps.fetchImpl=async(_,init)=>new Promise((resolve,reject)=>init.signal.addEventListener('abort',()=>reject(init.signal.reason)));
 await handleZen(s.event,s.responder,s.deps);assert.equal(s.out.status,403);
});
test('Bedrock credit denial does not revoke included Muse; generic denial still does',async()=>{
 const s=setup();s.deps.ddb.getItem=async p=>({Item:p.TableName==='tokens'?{owner_id:{S:'org_one'},bedrock_denied:{BOOL:true}}:{subscription_status:{S:'active'},provider_choice:{S:'opencode_zen'}}});await handleZen(s.event,s.responder,s.deps);assert.equal(s.out.status,200);
});
test('stateless tool replay accepts full function output without a prior-response lookup',async()=>{
 const s=setup();s.event.body=JSON.stringify({model:'muse-spark-1.3',store:false,input:[{role:'user',content:'run a tool'}, {type:'function_call',call_id:'call_1',name:'terminal',arguments:'{}'},{type:'function_call_output',call_id:'call_1',output:'done'}]});
 s.deps.fetchImpl=async(_,req)=>{const b=JSON.parse(req.body);assert.equal(b.store,false);assert.equal(b.previous_response_id,undefined);assert.equal(b.input[2].call_id,'call_1');return new Response('{"status":"completed"}');};await handleZen(s.event,s.responder,s.deps);assert.equal(s.out.status,200);
});
