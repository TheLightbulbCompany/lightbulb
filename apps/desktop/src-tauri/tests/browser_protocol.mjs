// Explicit, disposable headless Chromium acceptance. Never attaches personal Chrome.
// Run with vendored Node22 and OPENCLAW_PACKAGE_ROOT pointing at the read-only package.
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, chmod, access, readdir } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';
import { verifyBrowserPin } from '../scripts/verify-browser-pin.mjs';
import { extensionFixture } from './browser_extension_fixture.mjs';

const pkg = process.env.OPENCLAW_PACKAGE_ROOT;
await verifyBrowserPin(pkg);
assert.equal(process.version, 'v22.23.2');
const root = await mkdtemp(join(tmpdir(), 'lightbulb-browser-protocol-'));
const artifactName=`lightbulb-protocol-${randomUUID()}`;
const ownedArtifacts=[];
process.env.OPENCLAW_STATE_DIR = root;
process.env.OPENCLAW_HOME = root;
process.env.OPENCLAW_CONFIG_PATH = join(root, 'openclaw.json');
process.env.OPENCLAW_OAUTH_DIR = join(root, 'credentials');
await writeFile(join(root,'mcp-stub'), '#!/bin/sh\n: > "$OPENCLAW_STATE_DIR/mcp-started"\nexit 1\n');
await chmod(join(root,'mcp-stub'),0o700);
const server = createServer((req, res) => {
  if(req.url==='/download') {
    res.setHeader('Content-Type','application/octet-stream');
    res.setHeader('Content-Disposition','attachment; filename="known.bin"');
    res.end(Buffer.alloc(131072,66));return;
  }
  res.setHeader('Content-Type', 'text/html');
  res.end('<!doctype html><title>Lightbulb isolated protocol test</title><label>Name<input aria-label="Name"></label><button onclick="document.querySelector(\'output\').textContent=\'Clicked exactly once\'">Submit</button><output>Ready</output><input type="file" aria-label="Upload"><a href="/download" download>Download</a>');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
await writeFile(process.env.OPENCLAW_CONFIG_PATH, JSON.stringify({
  browser: { headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ssrfPolicy: { allowedHostnames: ['127.0.0.1'] },
    profiles: { lightbulb: { driver: 'openclaw', cdpPort: port + 1, headless: true },
      chrome: {driver:'extension',cdpPort:port+2},
      blocked: {driver:'existing-session',mcpCommand:join(root,'mcp-stub')} } },
  nodeHost: { browserProxy: { allowProfiles: ['lightbulb','chrome','blocked'] } },
}));
const load = name => import(pathToFileURL(join(pkg, 'dist', name)).href);
let stop;
try {
  const { i: invoke } = await load('plugin-service-DNzntOkw.js');
  stop = (await load('control-service-DqsDOL8Y.js')).n;
  const {a:stageUpload,t:discardUpload,n:recoverUploads}=await load('browser-proxy-upload-BSu6evh6.js');
  const uploadDir=join(root,'upload-cleanup-fixture');
  const stagingRequest={method:'POST',path:'/hooks/file-chooser',body:{ref:'e1'},uploadDir,
    upload:{envelope:'browser-upload-v1',files:[{name:'known.txt',contentBase64:'a25vd24='}]}};
  const staged=await stageUpload(stagingRequest);
  await discardUpload(staged);
  assert.equal(await access(staged.directory).then(()=>true,()=>false),false);
  const retained=await stageUpload(stagingRequest);
  await recoverUploads({uploadDir,nowMs:Date.now()+24*60*60*1000+1000});
  assert.equal(await access(retained.directory).then(()=>true,()=>false),false);
  const preflight=JSON.parse(await invoke(JSON.stringify({method:'POST',path:'/tabs/focus',body:{},profile:'blocked',errorEnvelope:'browser-v1'})));
  assert.deepEqual(preflight,{error:{status:400,body:{error:'targetId is required'}},route:{status:'resolved',profile:'blocked',driver:'existing-session'}});
  assert.equal(await access(join(root,'mcp-started')).then(()=>true,()=>false),false);
  const call = async (method, path, body, query = {}, command = 'browser.proxy', upload) => {
    const reply = JSON.parse(await invoke(JSON.stringify({ method, path, body, query, profile: 'lightbulb',
      timeoutMs: 60000, errorEnvelope: 'browser-v1', ...(upload ? { upload } : {}) }), command));
    assert.equal(reply.route.profile, 'lightbulb');
    assert.equal(reply.route.driver, 'openclaw');
    assert.equal(reply.error, undefined, JSON.stringify(reply.error));
    return reply;
  };
  const status = await call('GET', '/');
  assert.equal(status.result.running, false);
  const opened = await call('POST', '/tabs/open', { url: `http://127.0.0.1:${port}/` });
  const targetId = opened.result.targetId;
  assert.match(targetId, /^[A-F0-9]{32}$/);
  const snap = await call('GET', '/snapshot', undefined, {targetId, format: 'ai', maxChars: 32000, limit: 500});
  const refs = snap.result.refs;
  const input = Object.keys(refs).find(ref => refs[ref].name === 'Name');
  await call('POST','/act',{targetId,kind:'type',ref:input,text:'Scoped Mac browser'});
  const ref = Object.keys(refs).find(ref => refs[ref].name === 'Submit');
  assert.ok(ref, JSON.stringify(snap.result));
  await call('POST', '/act', {targetId, kind: 'click', ref});
  const after = await call('GET', '/snapshot', undefined, {targetId, format:'ai'});
  assert.match(after.result.snapshot, /Clicked exactly once/);
  const screenshot = await call('POST', '/screenshot', {targetId, type: 'jpeg', fullPage: false});
  assert.ok(screenshot.files[0].base64.length > 100);
  const pdf = await call('POST', '/pdf', {targetId});
  assert.ok(Buffer.from(pdf.files[0].base64, 'base64').subarray(0,5).equals(Buffer.from('%PDF-')));
  const downloadRef=Object.keys(after.result.refs).find(ref=>after.result.refs[ref].name==='Download');
  const download=await call('POST','/download',{targetId,ref:downloadRef,path:`${artifactName}.bin`});
  ownedArtifacts.push(download.result.download.path);
  assert.deepEqual(Buffer.from(download.files[0].base64,'base64'),Buffer.alloc(131072,66));
  const uploadRef = Object.keys(after.result.refs).find(ref => after.result.refs[ref].name === 'Upload');
  assert.ok(uploadRef);
  await call('POST', '/hooks/file-chooser', {targetId, ref:uploadRef}, {}, 'browser.proxy.upload.v1', {
    envelope:'browser-upload-v1', files:[{name:`${artifactName}.txt`, contentBase64: Buffer.alloc(131072,65).toString('base64')}],
  });
  const {r:uploads}=await load('paths-KitKkhYf.js');
  for(const directory of await readdir(join(uploads,'.proxy-uploads'))) {
    const owned=join(uploads,'.proxy-uploads',directory);
    if(await access(join(owned,'0',`${artifactName}.txt`)).then(()=>true,()=>false)) ownedArtifacts.push(owned);
  }
  await extensionFixture({pkg,root,chromePort:port+1,relayPort:port+2,targetId,url:`http://127.0.0.1:${port}/`,invoke});
  await call('DELETE', `/tabs/${targetId}`, undefined, {targetIdMode:'raw'});
  const tabs = await call('GET', '/tabs');
  assert.ok(!tabs.result.tabs.some(tab => tab.targetId === targetId));
  console.log('Pinned managed protocol: status/open/snapshot/ref-type/click/screenshot/PDF/128KiB upload/download/close passed');
} finally {
  if (stop) await stop();
  for(const artifact of ownedArtifacts) await rm(artifact,{recursive:true,force:true});
  await new Promise(resolve => server.close(resolve));
  await rm(root, { recursive: true, force: true });
}
// The bundled plugin owns background housekeeping timers outside the browser
// service. Our owned browser, fixture server and temporary files are closed above.
process.exit(0);
