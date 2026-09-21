export function abortOnClose(stream) {
  const controller = new AbortController();
  stream.once('close', () => controller.abort());
  return controller.signal;
}
