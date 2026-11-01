#!/usr/bin/env node
// One-command launcher: installs uv (which brings its own Python) if needed,
// runs OpenRing from PyPI, and opens the dashboard when the server is up.
"use strict";
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

// The PyPI release with this launcher's own version; OPENRING_SOURCE runs a checkout or a git URL instead.
const VERSION = require("../package.json").version;
const SOURCE = process.env.OPENRING_SOURCE;
const PACKAGE = SOURCE ? `openring[kokoro,whisper] @ ${SOURCE}` : `openring[kokoro,whisper]==${VERSION}`;
// Flags such as `--port 8001` go on to `openring`; the launcher reads the port to open the browser.
const userArgs = process.argv.slice(2);
const portArg = userArgs.find((_, i) => userArgs[i - 1] === "--port") || (userArgs.find((a) => a.startsWith("--port=")) || "").slice(7);
const PORT = portArg || process.env.PORT || "8000";
const isWin = process.platform === "win32";

function findUv() {
  const candidates = ["uv"];
  const home = os.homedir();
  candidates.push(path.join(home, ".local", "bin", isWin ? "uv.exe" : "uv"));
  candidates.push(path.join(home, ".cargo", "bin", isWin ? "uv.exe" : "uv"));
  for (const c of candidates) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

function installUv() {
  console.log("  Installing uv (Python manager, one time only)...");
  const r = isWin
    ? spawnSync("powershell", ["-ExecutionPolicy", "ByPass", "-NoProfile", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"], { stdio: "inherit" })
    : spawnSync("sh", ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], { stdio: "inherit" });
  if (r.status !== 0) {
    console.error("\n  Could not install uv automatically. Install it from https://docs.astral.sh/uv/ and run this command again.");
    process.exit(1);
  }
}

function openBrowser(url) {
  if (process.env.OPENRING_NO_BROWSER) return;
  const cmd = isWin ? ["cmd", ["/c", "start", "", url]] : process.platform === "darwin" ? ["open", [url]] : ["xdg-open", [url]];
  try { spawn(cmd[0], cmd[1], { stdio: "ignore", detached: true }).unref(); } catch (_) {}
}

function waitForServer(url, tries = 600) {
  return new Promise((resolve) => {
    const tick = (n) => {
      http.get(url + "/health", (res) => { res.resume(); res.statusCode === 200 ? resolve(true) : retry(n); }).on("error", () => retry(n));
    };
    const retry = (n) => (n <= 0 ? resolve(false) : setTimeout(() => tick(n - 1), 1000));
    tick(tries);
  });
}

let uv = findUv();
if (!uv) { installUv(); uv = findUv(); }
if (!uv) { console.error("  uv was installed but is not on PATH yet. Open a new terminal and run the command again."); process.exit(1); }

// Right after a release, uv may still hold an older copy of PyPI's index and answer that this version doesn't
// exist. Then ask PyPI again, once. (Refreshing on every start would break starting without internet.)
const STALE_INDEX = /no version of openring|No solution found when resolving/i;
const colors = process.stderr.isTTY ? { LOGURU_COLORIZE: "True" } : {};

function run(refresh) {
  const args = ["tool", "run", ...(process.stderr.isTTY ? ["--color", "always"] : []),
    ...(refresh ? ["--refresh-package", "openring"] : []), "--python", "3.12", "--from", PACKAGE, "openring", ...userArgs];
  // stderr passes through as it comes; the launcher also reads it to spot the stale index.
  const child = spawn(uv, args, { stdio: ["inherit", "inherit", "pipe"], env: { ...process.env, PORT, ...colors } });
  let seen = "";
  child.stderr.on("data", (chunk) => {
    process.stderr.write(chunk);
    if (seen.length < 20000) seen += chunk.toString();
  });
  child.on("exit", (code) => {
    if (code && !refresh && !SOURCE && STALE_INDEX.test(seen)) {
      console.log(`\n  OpenRing ${VERSION} is brand new; asking PyPI again...\n`);
      current = run(true);
      return;
    }
    process.exit(code ?? 0);
  });
  return child;
}

console.log("\n  Starting OpenRing (the first run downloads dependencies and can take a few minutes)...\n");
let current = run(false);
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => current.kill(sig));

const url = `http://localhost:${PORT}`;
waitForServer(url).then((up) => { if (up) openBrowser(url); });
