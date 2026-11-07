// `pnpm start -p <port>` appends "-p <port>"; serve() also sets PORT.
import { createServer } from "node:http";
createServer((_, res) => res.end("ok")).listen(Number(process.env.PORT));
