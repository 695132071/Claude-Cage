import test from "node:test";
import assert from "node:assert/strict";
import { cpSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { pathToFileURL } from "node:url";

// Applies internal/cage/novnc-patch.py to a private copy of the vendored noVNC
// and drives the patched RFB._handleMouse with the events a host WebView sends.
const root = realpathSync(tmpdir());
const work = mkdtempSync(join(root, "novnc-mouse-"));
assert(work.startsWith(root + sep));
test.after(() => rmSync(work, { recursive: true, force: true }));

function patchedNoVNC() {
  const target = join(work, "novnc");
  cpSync(new URL("../vendor/novnc", import.meta.url), target, { recursive: true });
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  try {
    execFileSync(python, [new URL("../../internal/cage/novnc-patch.py", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"), target], { stdio: "pipe" });
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
  return target;
}

let loaded;
async function loadRFB(t) {
  loaded ||= importRFB();
  const RFB = await loaded;
  if (!RFB) t.skip("Python is not available to apply the noVNC patch");
  return RFB;
}

async function importRFB() {
  const target = patchedNoVNC();
  if (!target) return null;
  const noop = () => {};
  // Just enough of a browser for the module graph to load.
  Object.assign(globalThis, { window: globalThis, addEventListener: noop, removeEventListener: noop,
    MutationObserver: class { observe() {} disconnect() {} } });
  globalThis.document = { documentElement: {}, body: { appendChild: noop, removeChild: noop },
    addEventListener: noop, removeEventListener: noop, getElementById: () => null,
    createElement: () => ({ style: {}, getContext: () => ({}), appendChild: noop, remove: noop }) };
  const { log, error } = console;
  Object.assign(console, { log: noop, error: noop });
  try {
    return (await import(pathToFileURL(join(target, "core/rfb.js")).href)).default;
  } finally { Object.assign(console, { log, error }); }
}

function mouse(RFB) {
  const canvas = { setCapture() {}, getBoundingClientRect: () => ({ left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 }) };
  const masks = [];
  const rfb = { _canvas: canvas, dragViewport: false, _handleMouseButton: (_x, _y, mask) => masks.push(mask), _handleMouseMove() {} };
  const send = (type, button, buttons) => RFB.prototype._handleMouse.call(rfb, {
    type, button, buttons, clientX: 40, clientY: 30, target: canvas, stopPropagation() {}, preventDefault() {},
  });
  return { masks, send };
}

test("a click is pressed and released even when WebKit reports stale buttons", async (t) => {
  const RFB = await loadRFB(t);
  if (!RFB) return;
  // macOS WebKit reads buttons from [NSEvent pressedMouseButtons] while it
  // handles the event: a quick tap or a synthesized click reads 0 on press.
  let { masks, send } = mouse(RFB);
  send("mousedown", 0, 0);
  send("mouseup", 0, 0);
  assert.deepEqual(masks, [1, 0]);
  // A release handled after the next press began still reads the button held.
  ({ masks, send } = mouse(RFB));
  send("mousedown", 0, 1);
  send("mouseup", 0, 1);
  assert.deepEqual(masks, [1, 0]);
  // Right and middle buttons use the RFB bit order.
  ({ masks, send } = mouse(RFB));
  send("mousedown", 2, 0);
  send("mouseup", 2, 0);
  send("mousedown", 1, 0);
  send("mouseup", 1, 0);
  assert.deepEqual(masks, [4, 0, 2, 0]);
});

test("accurate button state from Chromium and WebView2 is unchanged", async (t) => {
  const RFB = await loadRFB(t);
  if (!RFB) return;
  const { masks, send } = mouse(RFB);
  send("mousedown", 0, 1);
  send("mousedown", 2, 3);
  send("mouseup", 2, 1);
  send("mouseup", 0, 0);
  assert.deepEqual(masks, [1, 5, 1, 0]);
});
