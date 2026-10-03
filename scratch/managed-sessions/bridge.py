"""Serve index.html on http://localhost:8765 and relay one browser WebSocket to the held session."""
import asyncio, os, pathlib, websockets
from websockets.http11 import Response
from websockets.datastructures import Headers
SOCK = "/private/tmp/lbh-manual.sock"; PAGE = (pathlib.Path(__file__).parent / "index.html").read_bytes()
def http(conn, req):
    if req.path == "/":
        return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8")]), PAGE)
async def relay(ws):
    r, w = await asyncio.open_unix_connection(SOCK)
    async def up():
        async for m in ws: w.write(m); await w.drain()
    async def down():
        while d := await r.read(65536): await ws.send(d)
    await asyncio.gather(up(), down(), return_exceptions=True)
async def main():
    async with websockets.serve(relay, "localhost", 8765, process_request=http, max_size=None):
        print("open http://localhost:8765 in Safari"); await asyncio.Future()
asyncio.run(main())
