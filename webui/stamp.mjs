// The committed build carries a hash of what it was built from, so CI can tell it was rebuilt after a change without
// needing the same bytes: a mirror a patch behind minifies the same code with other names.
// `node stamp.mjs write` (after `vite build`) or `node stamp.mjs check`.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";

const HERE = import.meta.dirname;
const STAMP = join(HERE, "../src/voice_assistant/webui_static/source.txt");
const FILES = [
  "index.html",
  "package.json",
  "package-lock.json",
  "vite.config.ts",
  "tsconfig.json",
  "tsconfig.node.json",
];
const NOT_BUILT = /(\.test\.ts|test-setup\.ts)$/;

function sourceHash() {
  // The files git has or would add, not a stray .DS_Store, so this machine's hash is CI's once they're committed.
  const listed = ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "src"];
  const src = execFileSync("git", listed, { cwd: HERE, encoding: "utf8" })
    .split("\0")
    .filter(f => f && !NOT_BUILT.test(f) && !basename(f).startsWith(".") && existsSync(join(HERE, f)));
  const hash = createHash("sha256");
  for (const file of [...FILES, ...src].sort()) {
    hash
      .update(`${file}\0`)
      .update(readFileSync(join(HERE, file)))
      .update("\0");
  }
  return hash.digest("hex");
}

if (process.argv[2] === "write") {
  writeFileSync(STAMP, `${sourceHash()}\n`);
} else if (process.argv[2] === "check") {
  let built = "";
  try {
    built = readFileSync(STAMP, "utf8").trim();
  } catch {}
  if (built !== sourceHash()) {
    process.stderr.write("webui/ changed since src/voice_assistant/webui_static/ was built: run npm run build.\n");
    process.exit(1);
  }
} else {
  process.stderr.write("usage: node stamp.mjs write|check\n");
  process.exit(2);
}
