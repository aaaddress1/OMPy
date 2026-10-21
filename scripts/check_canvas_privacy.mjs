/** Verify the pinned Canvas's telemetry configuration and actual served HTML. */
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const canvas = resolve(".tools/canvas-runtime/node_modules/@openhands/agent-canvas");
const moduleAt = (file) => import(pathToFileURL(resolve(canvas, "scripts", file)).href);
const { buildAgentServerTelemetryEnv } = await moduleAt("dev-safe.mjs");
const { buildAutomationTelemetryEnv } = await moduleAt("dev-with-automation.mjs");
const { parseArgs, startStaticServer } = await moduleAt("static-server.mjs");

assert.equal(process.env.DO_NOT_TRACK, "1");
assert.equal(process.env.VITE_DO_NOT_TRACK, "1");
assert.equal(process.env.AGENT_CANVAS_DISABLE_TELEMETRY, "1");
const backend = buildAgentServerTelemetryEnv(process.env);
assert.equal(backend.DO_NOT_TRACK, "1");
assert.equal(backend.OH_TELEMETRY_EXPORTER, "none");
assert.ok(!backend.OH_TELEMETRY_POSTHOG_API_KEY && !backend.OH_TELEMETRY_HTTP_TOKEN);
assert.deepEqual(buildAutomationTelemetryEnv(process.env), {});

const config = parseArgs(["--dir", resolve(canvas, "build"), "--port", "0", "--host", "127.0.0.1"], process.env);
assert.equal(config.disableTelemetry, true);
const server = await startStaticServer(config);
try {
  const response = await fetch(`http://127.0.0.1:${server.address().port}/canvas`);
  assert.equal(response.status, 200);
  assert.ok((await response.text()).includes("window.__AGENT_CANVAS_DO_NOT_TRACK__=true;"));
  console.log("Canvas frontend opt-out, backend exporter=none, automation analytics disabled: passed.");
} finally {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
