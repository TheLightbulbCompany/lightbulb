import https from 'node:https';

// The response is passed through byte for byte; only a bounded SSE line buffer
// observes generation IDs for the durable settlement worker.
export function forward({ body, key, writeHead, write, waitForDrain, onGenerationId, end, abortSignal, requestImpl = https.request }) {
  return new Promise((resolve, reject) => {
    let pending = Promise.resolve();
    const rejectAfterPending = error => { pending.then(() => reject(error), () => reject(error)); };
    const req = requestImpl('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST', signal: abortSignal,
      headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json', accept: 'text/event-stream' },
    }, upstream => {
      const status = upstream.statusCode ?? 502;
      try { writeHead(status, upstream.headers); } catch (error) { req.destroy(error); reject(error); return; }
      let line = '', generationId, model, notified = false, buffered = [], bufferedBytes = 0, errorBody = '';
      upstream.on('data', chunk => {
        // Kept (bounded) so the gate can tell a per-key daily limit from other 403s.
        if (status !== 200 && errorBody.length < 8192) errorBody += chunk.toString('utf8');
        if (status === 200) {
          upstream.pause?.();
          pending = pending.then(async () => {
            if (!generationId) {
              buffered.push(chunk);
              bufferedBytes += chunk.length;
              if (bufferedBytes > 65536) throw new Error('Generation ID missing from stream prefix');
              line = (line + chunk.toString('utf8')).slice(-65536);
              for (const match of line.matchAll(/(?:^|\n)data:\s*(\{[^\n]+\})/g)) {
                try { const j = JSON.parse(match[1]); generationId = j.id || generationId; model = model || j.model; } catch { /* partial line */ }
              }
              if (!generationId) {
                const head = line.split('"choices"')[0];
                generationId = /(?:^|\n)data:\s*\{[^\n]*?"id"\s*:\s*"([^"\\\n]+)"/.exec(head)?.[1];
              }
            }
            if (!generationId) { upstream.resume?.(); return; }
            if (generationId && !notified && onGenerationId) { await onGenerationId(generationId); notified = true; }
            const toWrite = buffered.length ? buffered : [chunk];
            buffered = [];
            bufferedBytes = 0;
            for (const part of toWrite) {
              if (write(part) === false) {
                if (!waitForDrain) throw new Error('Client backpressure cannot be drained');
                await waitForDrain();
              }
            }
            upstream.resume?.();
          }).catch(error => { req.destroy(error); reject(error); });
        }
      });
      upstream.on('end', () => { pending.then(() => { if (status !== 200) write(Buffer.from(JSON.stringify({ error: { message: `Upstream returned ${status}` } }))); end(errorBody.slice(0, 8192)); resolve({ status, generationId, model, preGeneration: [400, 401, 402, 403, 404, 422, 429].includes(status) }); }).catch(reject); });
      upstream.on('error', rejectAfterPending);
    });
    req.on('error', rejectAfterPending);
    req.end(JSON.stringify(body));
  });
}
