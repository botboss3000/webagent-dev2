#!/usr/bin/env node
/*
 * Front-end JS syntax checker — the cheap guard that stops a parse error (e.g.
 * a duplicate `const` declaration) from ever reaching a browser, where it would
 * silently kill the boot chain and leave the app stuck on a spinner.
 *
 * How it works: every front-end .js file is parsed by Node as an ES module
 * (`node --input-type=module --check`). That is a pure SYNTAX check — nothing
 * runs, no DOM/browser globals are needed, so it is safe for every file and
 * takes a few milliseconds each. A duplicate declaration, an unbalanced brace,
 * a stray keyword, etc. all fail here with the exact file + line.
 *
 * Usage:
 *   node scripts/check-js-syntax.mjs            # check all front-end JS
 *   node scripts/check-js-syntax.mjs a.js b.js  # check specific files
 *
 * Exit code 0 = all clean, 1 = at least one file failed (prints the errors).
 * Wired into .git/hooks/pre-commit so a broken file can't be committed.
 */

import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve, relative } from "node:path";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Directories/files scanned by default. Add more roots here if the UI grows.
const DEFAULT_ROOTS = ["ui", "sw.js"];

function walk(path, out) {
  let st;
  try { st = statSync(path); } catch { return; }
  if (st.isDirectory()) {
    for (const name of readdirSync(path)) {
      if (name === "node_modules" || name.startsWith(".")) continue;
      walk(join(path, name), out);
    }
  } else if (path.endsWith(".js")) {
    out.push(path);
  }
}

function collectDefault() {
  const out = [];
  for (const r of DEFAULT_ROOTS) {
    const p = join(ROOT, r);
    if (existsSync(p)) walk(p, out);
  }
  return out;
}

function checkFile(file) {
  let code;
  try { code = readFileSync(file, "utf8"); } catch (e) {
    return Promise.resolve({ ok: false, error: `cannot read file: ${e.message}` });
  }
  return new Promise((resolveResult) => {
    const child = spawn(process.execPath, ["--input-type=module", "--check"], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "", stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => resolveResult({ ok: false, error: error.message }));
    child.on("close", (status) => resolveResult(status === 0
      ? { ok: true }
      : { ok: false, error: (stderr || stdout || "syntax error").trim() }));
    child.stdin.end(code);
  });
}

const args = process.argv.slice(2);
const files = (args.length ? args.map((a) => resolve(a)) : collectDefault());

// Starting a fresh Node parser is the expensive part. Keep several busy at once
// without launching hundreds simultaneously, which is slower on Windows.
const concurrency = Math.min(8, files.length || 1);
const results = new Array(files.length);
let next = 0;
async function worker() {
  for (;;) {
    const index = next++;
    if (index >= files.length) return;
    results[index] = await checkFile(files[index]);
  }
}
await Promise.all(Array.from({ length: concurrency }, worker));

let failures = 0;
for (let index = 0; index < files.length; index++) {
  const file = files[index];
  const { ok, error } = results[index];
  if (!ok) {
    failures++;
    console.error(`\n✗ ${relative(ROOT, file)}`);
    console.error(error.split("\n").map((l) => "    " + l).join("\n"));
  }
}

if (failures) {
  console.error(`\n${failures} file(s) failed the JS syntax check. Commit blocked.`);
  process.exit(1);
}
console.log(`✓ JS syntax OK (${files.length} files checked)`);
