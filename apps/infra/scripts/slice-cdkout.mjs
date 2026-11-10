// Cut cdk.out into per-asset slices so each deploy.yml asset job downloads its
// own asset, not the whole ~40 MB cloud assembly (see "cdk.out transport" in
// lib/app.ts). Writes <out>/base.tgz (cdk.out minus every asset.* entry: the
// manifests, templates and publish scripts) and <out>/<assetId>.tgz for every
// asset id in every *.assets.json. A template asset's source lives in the base,
// so its slice is an empty archive; a job always finds its slice or fails.
//
// Usage (from apps/infra): node scripts/slice-cdkout.mjs cdk.out <out>
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const [cdkout, out] = process.argv.slice(2);
if (!cdkout || !out) throw new Error("usage: slice-cdkout.mjs <cdk.out> <outdir>");
const parent = path.dirname(path.resolve(cdkout));
const name = path.basename(path.resolve(cdkout));
fs.mkdirSync(out, { recursive: true });

const tar = (file, entries, extra = []) =>
  execFileSync("tar", ["-czf", path.resolve(out, file), ...extra, "-C", parent, ...(entries.length ? entries : ["-T", "/dev/null"])]);

tar("base.tgz", [name], [`--exclude=${name}/asset.*`]);

// Manifests sit at the root and one level down (assembly-<stage>/), never inside an asset.
const dirs = [cdkout, ...fs.readdirSync(cdkout).filter((d) => d.startsWith("assembly-")).map((d) => path.join(cdkout, d))];
let count = 0;
for (const dir of dirs) {
  for (const file of fs.readdirSync(dir).filter((f) => f.endsWith(".assets.json"))) {
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, file), "utf8"));
    for (const [id, asset] of [...Object.entries(manifest.files ?? {}), ...Object.entries(manifest.dockerImages ?? {})]) {
      const source = asset.source.path ?? asset.source.directory;
      if (!source) throw new Error(`${file}: asset ${id} has no path/directory source`);
      const rel = path.relative(path.resolve(cdkout), path.resolve(dir, source));
      if (rel.startsWith("..")) throw new Error(`${file}: asset ${id} source ${source} is outside cdk.out`);
      tar(`${id}.tgz`, rel.startsWith("asset.") ? [path.join(name, rel)] : []);
      count += 1;
    }
  }
}
console.log(`cdk.out sliced: base + ${count} asset slices in ${out}`);
