// Protocol fixture: real pinned relay/Playwright + disposable Chrome CDP. The
// extension's OS permission/tab-group UI is deliberately not impersonated here.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function extensionFixture({pkg, root, chromePort, relayPort, targetId, url, invoke}) {
  const require = createRequire(join(pkg, 'package.json'));
  const WebSocket = require('ws');
  const load = name => import(pathToFileURL(join(pkg,'dist',name)).href);
  const {createExtensionRelayAuthClient, EXTENSION_RELAY_V2_PROTOCOL} = await load('extensions/browser/chrome-extension/modules/relay-auth-v2.js');
  const {parsePairingString} = await load('extensions/browser/chrome-extension/modules/relay-core.js');
  const token = (await readFile(join(root,'credentials/browser-extension-relay.secret'),'utf8')).trim();
  const relayUrl = `ws://127.0.0.1:${relayPort}/extension`;
  assert.equal(parsePairingString(`${relayUrl}#${token}`).relayUrl, relayUrl);
  const auth = await createExtensionRelayAuthClient({token, relayUrl});
  const version = await (await fetch(`http://127.0.0.1:${chromePort}/json/version`)).json();
  const cdp = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => {cdp.once('open',resolve);cdp.once('error',reject);});
  let serial = 0, chromeSession;
  const pending = new Map();
  let extension;
  const send = (method, params={}, sessionId) => new Promise((resolve,reject) => {
    const id = ++serial;
    pending.set(id,{resolve,reject});
    cdp.send(JSON.stringify({id,method,params,...(sessionId ? {sessionId} : {})}));
  });
  cdp.on('message', raw => {
    const message=JSON.parse(raw.toString());
    if(message.id) {
      const waiter=pending.get(message.id);pending.delete(message.id);
      if(message.error) waiter?.reject(new Error(JSON.stringify(message.error)));
      else waiter?.resolve(message.result);
    } else if(message.sessionId===chromeSession && extension?.readyState===WebSocket.OPEN) {
      extension.send(JSON.stringify({type:'cdpEvent',tabId:7,method:message.method,params:message.params}));
    }
  });
  extension = new WebSocket(relayUrl,EXTENSION_RELAY_V2_PROTOCOL,{origin:'chrome-extension://kcdjddhmeafeomebliikmbpblkmkfoig'});
  const ready = new Promise((resolve,reject) => {
    extension.once('error',reject);
    extension.once('open',()=>extension.send(JSON.stringify(auth.start())));
    extension.on('message', async raw => {
      const message=JSON.parse(raw.toString());
      try {
        if(message.type==='auth.challenge') {extension.send(JSON.stringify(await auth.acceptChallenge(message)));return;}
        if(message.type==='auth.ok') {
          await auth.acceptOk(message);
          extension.send(JSON.stringify({type:'hello',userAgent:'isolated protocol fixture',browserVersion:version.Browser,
            extensionVersion:'2026.8.1',tabs:[{tabId:7,url,title:'Existing fixture tab',active:true}]}));
          resolve(); return;
        }
        let result={};
        if(message.type==='ping') {extension.send(JSON.stringify({type:'pong'}));return;}
        if(message.type==='attach') {
          assert.equal(message.tabId,7);
          chromeSession=(await send('Target.attachToTarget',{targetId,flatten:true})).sessionId;
          result={targetId};
        } else if(message.type==='cdp') {
          assert.equal(message.tabId,7);
          result=await send(message.method,message.params,chromeSession);
        } else if(message.type==='detach') {
          if(chromeSession) await send('Target.detachFromTarget',{sessionId:chromeSession});
          chromeSession=undefined;
        } else throw new Error(`Unexpected extension command ${message.type}`);
        extension.send(JSON.stringify({type:'result',seq:message.seq,result}));
      } catch(error) {reject(error);extension.send(JSON.stringify({type:'error',seq:message.seq,message:String(error)}));}
    });
  });
  const call=async(method,path,body,query={})=>JSON.parse(await invoke(JSON.stringify({method,path,body,query,
    profile:'chrome',timeoutMs:20000,errorEnvelope:'browser-v1'})));
  try {
    await ready;
    const tabs=await call('GET','/tabs');
    assert.equal(tabs.error,undefined,JSON.stringify(tabs));
    assert.equal(tabs.route.driver,'extension');
    assert.deepEqual(tabs.result.tabs.map(tab=>tab.targetId),[targetId]);
    const snap=await call('GET','/snapshot',undefined,{targetId,format:'ai'});
    assert.equal(snap.error,undefined,JSON.stringify(snap));
    assert.match(snap.result.snapshot,/Submit/);
    extension.send(JSON.stringify({type:'tabs',tabs:[]}));
    const revoked=await call('GET','/snapshot',undefined,{targetId,format:'ai'});
    assert.ok(revoked.error,JSON.stringify(revoked));
    assert.ok((await send('Target.getTargets')).targetInfos.some(tab=>tab.targetId===targetId),'revocation must preserve personal tab');
    console.log('Pinned extension protocol: v2 authentication/native existing target/snapshot/revoke/preserve tab passed');
  } finally {extension.close();cdp.close();}
}
