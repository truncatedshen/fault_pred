#!/usr/bin/env node
// Real-Chromium acceptance check for the visual designer.
//
// It starts a throwaway platform server, drives Google Chrome headless through the
// DevTools protocol, and verifies registry wiring, layout geometry, example execution,
// node dragging, zooming and result rendering. Screenshots are written to
// .fault-platform/screenshots for manual review.

const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const {spawn} = require("node:child_process");

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const NEW_COMPONENTS = ["feature.spectral", "feature.score_select", "feature.pca"];
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
];

function findChrome() {
  for (const candidate of CHROME_CANDIDATES.filter(Boolean)) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error("Chrome was not found; set CHROME_PATH to run the browser check");
}

async function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer().listen(0, "127.0.0.1", () => {
      const {port} = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function until(predicate, message, attempts = 250) {
  for (let index = 0; index < attempts; index++) {
    if (await predicate()) return;
    await pause(60);
  }
  throw new Error(message);
}

function check(condition, message, detail = "") {
  if (!condition) throw new Error(detail ? `${message}: ${detail}` : message);
}

class Client {
  constructor(socket) {
    this.socket = socket;
    this.counter = 0;
    this.pending = new Map();
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      const entry = this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(message.error.message));
      else entry.resolve(message.result);
    };
  }

  static async connect(port, match = (target) => target.type === "page") {
    await until(async () => {
      try {
        return (await fetch(`http://127.0.0.1:${port}/json/version`)).ok;
      } catch {
        return false;
      }
    }, "Chrome DevTools endpoint did not start");
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const target = targets.find(match);
    if (!target) throw new Error("No debuggable page target found");
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = () => reject(new Error("DevTools websocket failed"));
    });
    return new Client(socket);
  }

  send(method, params = {}) {
    const id = ++this.counter;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {resolve, reject});
      this.socket.send(JSON.stringify({id, method, params}));
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (result.exceptionDetails) {
      const details = result.exceptionDetails;
      throw new Error(details.exception?.description || details.text || "evaluate failed");
    }
    return result.result.value;
  }

  async mouse(type, x, y, extra = {}) {
    await this.send("Input.dispatchMouseEvent", {
      type, x, y, button: "left",
      buttons: type === "mouseReleased" ? 0 : 1, clickCount: 1, ...extra,
    });
  }

  async click(x, y) {
    await this.mouse("mousePressed", x, y);
    await this.mouse("mouseReleased", x, y, {buttons: 0});
  }

  async screenshot(destination) {
    const {data} = await this.send("Page.captureScreenshot", {format: "png"});
    fs.writeFileSync(destination, Buffer.from(data, "base64"));
  }
}

/** A point inside the node that really hit-tests to it, not to an overlay or the pan area. */
function hitPoint(nodeId) {
  return `(() => { const node = document.querySelector('.node[data-id=' + ${JSON.stringify(JSON.stringify(nodeId))} + ']');
    if (!node) return null;
    const boxes = [node.querySelector(".node-title"), node].filter(Boolean).map((el) => el.getBoundingClientRect());
    for (const box of boxes) {
      for (const [fx, fy] of [[0.5, 0.5], [0.3, 0.5], [0.7, 0.5], [0.5, 0.25]]) {
        const x = box.left + box.width * fx, y = box.top + box.height * fy;
        const hit = document.elementFromPoint(x, y);
        // Ports are intentionally inert for selection and dragging.
        if (hit && hit.closest(".node") === node && !hit.closest(".port")) return {x, y};
      }
    }
    return null; })()`;
}

function nodePosition(nodeId) {
  return `(() => { const node = document.querySelector('.node[data-id=' + ${JSON.stringify(JSON.stringify(nodeId))} + ']');
    return node ? {left: parseFloat(node.style.left), top: parseFloat(node.style.top)} : null; })()`;
}

async function main() {
  const root = path.resolve(__dirname, "..");
  const python = path.join(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
  if (!fs.existsSync(python)) throw new Error("Virtual environment missing; run setup.ps1 first");
  const shootDir = path.join(root, ".fault-platform", "screenshots");
  fs.rmSync(shootDir, {recursive: true, force: true});
  fs.mkdirSync(shootDir, {recursive: true});
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "fault-browser-data-"));
  const storageRoot = fs.mkdtempSync(path.join(os.tmpdir(), "fault-browser-store-"));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fault-browser-profile-"));
  const httpPort = await freePort();
  const cdpPort = await freePort();
  const origin = `http://127.0.0.1:${httpPort}`;
  const server = spawn(
    python,
    ["-m", "fault_platform", "serve", "--port", String(httpPort), "--data-root", dataRoot,
      "--storage-root", storageRoot],
    {cwd: root, windowsHide: true, stdio: "ignore"},
  );
  const chrome = spawn(
    findChrome(),
    ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
      "--no-default-browser-check", "--window-size=1680,1050", `--remote-debugging-port=${cdpPort}`,
      `--user-data-dir=${profile}`, "about:blank"],
    {cwd: root, windowsHide: true, stdio: "ignore"},
  );
  const report = {origin, screenshots: [], checks: []};
  let client;
  try {
    await until(async () => {
      try {
        return (await fetch(`${origin}/api/health`)).ok;
      } catch {
        return false;
      }
    }, "test platform server did not start");
    const health = await (await fetch(`${origin}/api/health`)).json();
    if (health.components !== 29) throw new Error(`expected 29 components, got ${health.components}`);
    report.checks.push({name: "server exposes the full registry", components: health.components});

    client = await Client.connect(cdpPort);
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Page.navigate", {url: origin});
    await until(
      () => client.evaluate("document.readyState === 'complete' && document.querySelectorAll('.component-item').length === 29"),
      "component palette did not render in Chrome",
    );

    const palette = await client.evaluate(`(() => {
      const types = [...document.querySelectorAll(".component-item")].map((item) => item.dataset.type);
      const library = document.querySelector("#component-library").getBoundingClientRect();
      const canvas = document.querySelector("#canvas").getBoundingClientRect();
      const inspector = document.querySelector("#inspector-content").getBoundingClientRect();
      return {types, count: document.querySelector("#catalog-count").textContent,
        groups: document.querySelectorAll(".category-title").length,
        library: [library.width, library.height], canvas: [canvas.width, canvas.height],
        inspector: [inspector.width, inspector.height]}; })()`);
    const missing = NEW_COMPONENTS.filter((type) => !palette.types.includes(type));
    if (missing.length) throw new Error(`palette is missing ${missing.join(", ")}`);
    if (palette.groups !== 5) throw new Error(`expected 5 category groups, got ${palette.groups}`);
    for (const [name, box] of Object.entries({
      library: palette.library, canvas: palette.canvas, inspector: palette.inspector,
    })) {
      if (box[0] < 100 || box[1] < 100) throw new Error(`${name} has no usable layout size: ${box}`);
    }
    report.checks.push({
      name: "palette shows every category with the new components",
      catalog: palette.count, groups: palette.groups,
      library: palette.library, canvas: palette.canvas, inspector: palette.inspector,
    });
    await client.screenshot(path.join(shootDir, "01-catalog.png"));

    const created = await (await fetch(`${origin}/api/control/create_example`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
    })).json();
    const pipelineId = created.graph.id;
    await client.evaluate(`(async () => { const select = document.querySelector("#pipeline-list");
      select.dispatchEvent(new Event("focus")); await new Promise((r) => setTimeout(r, 900));
      select.value = ${JSON.stringify(pipelineId)}; select.dispatchEvent(new Event("change")); return true; })()`);
    await until(
      () => client.evaluate(`document.querySelectorAll(".node").length === ${created.graph.nodes.length}`),
      "example graph did not render on the canvas",
    );
    await client.evaluate("document.querySelector('#fit').click()");

    const canvasRect = await client.evaluate(`(() => { const box = document.querySelector("#canvas").getBoundingClientRect();
      return {x: box.left, y: box.top, width: box.width, height: box.height}; })()`);
    const fittedZoom = await client.evaluate("Number(document.querySelector('#zoom-label').textContent.replace('%', '')) / 100");
    const nodes = await client.evaluate(`[...document.querySelectorAll(".node")].map((node) => {
      const box = node.getBoundingClientRect();
      return {id: node.dataset.id, width: box.width, height: box.height,
        inside: box.left >= 0 && box.top >= 0 && box.right <= innerWidth && box.bottom <= innerHeight}; })`);
    // Compare in graph units: the canvas is fitted, so on-screen pixels are scaled by the zoom.
    const cramped = nodes.filter((node) => node.width / fittedZoom < 200 || node.height / fittedZoom < 40);
    if (cramped.length) throw new Error(`nodes rendered without a usable box at zoom ${fittedZoom}: ${JSON.stringify(cramped)}`);
    if (!nodes.every((node) => node.inside)) throw new Error("fit canvas left nodes outside the viewport");
    const edges = await client.evaluate(`[...document.querySelectorAll("#connections path[data-edge]")].map((path) => path.getAttribute("d").length)`);
    if (edges.length !== created.graph.edges.length || Math.min(...edges) < 20) {
      throw new Error(`edges not drawn: ${edges.length} paths for ${created.graph.edges.length} connections`);
    }
    report.checks.push({
      name: "example graph renders nodes and connection curves",
      nodes: nodes.length, edges: edges.length, zoom: fittedZoom,
      node_size_units: [Math.round(nodes[0].width / fittedZoom), Math.round(nodes[0].height / fittedZoom)],
      shortest_edge_path: Math.min(...edges),
    });
    await client.screenshot(path.join(shootDir, "02-example-graph.png"));

    let start = null;
    let dragId = null;
    for (const candidate of ["overview", "filter", "stat", ...nodes.map((node) => node.id)]) {
      start = await client.evaluate(hitPoint(candidate));
      if (start) {
        dragId = candidate;
        break;
      }
    }
    if (!start) throw new Error("no node could be hit-tested on the canvas");
    const before = await client.evaluate(nodePosition(dragId));
    const dx = 140;
    const dy = 90;
    await client.mouse("mousePressed", start.x, start.y);
    for (let step = 1; step <= 6; step++) {
      await client.mouse("mouseMoved", start.x + (dx * step) / 6, start.y + (dy * step) / 6);
    }
    await client.mouse("mouseReleased", start.x + dx, start.y + dy, {buttons: 0});
    await pause(400);
    const after = await client.evaluate(nodePosition(dragId));
    const zoom = await client.evaluate("Number(document.querySelector('#zoom-label').textContent.replace('%', '')) / 100");
    const moved = {x: after.left - before.left, y: after.top - before.top};
    if (Math.abs(moved.x - dx / zoom) > 4 || Math.abs(moved.y - dy / zoom) > 4) {
      throw new Error(`drag did not move ${dragId} as expected: ${JSON.stringify(moved)} at zoom ${zoom}`);
    }
    const stored = await (await fetch(`${origin}/api/control/get_pipeline`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({pipeline_id: pipelineId}),
    })).json();
    const persisted = stored.graph.nodes.find((node) => node.id === dragId).position;
    if (Math.abs(persisted.x - after.left) > 1 || Math.abs(persisted.y - after.top) > 1) {
      throw new Error(`server position ${JSON.stringify(persisted)} differs from canvas ${JSON.stringify(after)}`);
    }
    report.checks.push({
      name: "pointer drag of a node persists to the shared graph",
      node: dragId, zoom, canvas_delta: moved, stored_position: persisted,
    });

    const zoomBefore = zoom;
    await client.evaluate("document.querySelector('#zoom-in').click()");
    const zoomAfter = await client.evaluate("Number(document.querySelector('#zoom-label').textContent.replace('%', '')) / 100");
    if (!(zoomAfter > zoomBefore)) throw new Error("zoom control did not change the canvas scale");
    await client.evaluate("document.querySelector('#fit').click()");
    const zoomFitted = await client.evaluate("Number(document.querySelector('#zoom-label').textContent.replace('%', '')) / 100");
    if (!(zoomFitted <= zoomAfter)) throw new Error("fit canvas did not reset the scale");
    report.checks.push({name: "zoom and fit controls change the canvas scale", zoom: [zoomBefore, zoomAfter, zoomFitted]});

    // Place a new component through the real palette drop path and configure it.
    const knownIds = nodes.map((node) => node.id);
    await client.evaluate(`(() => { const canvas = document.querySelector("#canvas");
      const box = canvas.getBoundingClientRect();
      const data = new DataTransfer(); data.setData("component", "feature.spectral");
      const options = {bubbles: true, cancelable: true, dataTransfer: data,
        clientX: box.left + box.width * 0.42, clientY: box.top + box.height * 0.5};
      canvas.dispatchEvent(new DragEvent("dragover", options));
      canvas.dispatchEvent(new DragEvent("drop", options)); return true; })()`);
    await until(
      () => client.evaluate(`document.querySelectorAll(".node").length === ${knownIds.length + 1}`),
      "palette drop did not add a node",
    );
    const addedId = await client.evaluate(
      `[...document.querySelectorAll(".node")].map((n) => n.dataset.id).find((id) => !${JSON.stringify(knownIds)}.includes(id))`,
    );
    const addedPoint = await client.evaluate(hitPoint(addedId));
    if (addedPoint) await client.click(addedPoint.x, addedPoint.y);
    await until(
      async () => {
        const inspector = await client.evaluate("document.querySelector('#inspector-content').textContent");
        return inspector.includes("频域特征") && inspector.includes("sampling_rate") && inspector.includes("必填");
      },
      "new component did not expose its typed parameter form",
    );
    const inspectorText = await client.evaluate("document.querySelector('#inspector-content').textContent");
    const fields = await client.evaluate("document.querySelectorAll('#inspector-content .parameter').length");
    if (fields < 10) throw new Error(`spectral node rendered only ${fields} parameter fields`);
    await client.evaluate("document.querySelector('#delete').click()");
    await until(
      () => client.evaluate(`document.querySelectorAll(".node").length === ${knownIds.length}`),
      "deleting the new node did not restore the graph",
    );
    const afterDelete = await (await fetch(`${origin}/api/control/get_pipeline`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({pipeline_id: pipelineId}),
    })).json();
    if (afterDelete.graph.nodes.length !== knownIds.length) {
      throw new Error("server graph kept the deleted node");
    }
    report.checks.push({
      name: "palette drop, typed parameter form and delete work in Chrome",
      component: "feature.spectral", parameter_fields: fields,
      required_marking: inspectorText.includes("必填"),
    });

    // An Agent edit (MCP / HTTP) must show up without any user interaction in the page.
    const control = (operation, body) =>
      fetch(`${origin}/api/control/${operation}`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
      }).then((response) => response.json());
    const badgesBefore = await client.evaluate(
      "[...document.querySelectorAll('.node-status')].map((el) => el.textContent.trim())",
    );
    const remoteAdd = await control("add_component", {
      pipeline_id: pipelineId,
      component_type: "feature.spectral",
      node_id: "remote_spectral",
      parameters: {
        columns: ["vibration"], sampling_rate: 64.0, group_column: "equipment",
        window_size: 16, features: ["dominant_frequency"],
      },
    });
    check(remoteAdd.success, "remote add_component failed", JSON.stringify(remoteAdd));
    const syncStart = Date.now();
    await until(
      async () => (await client.evaluate("document.querySelectorAll('.node').length")) === knownIds.length + 1,
      "the page did not live-refresh after an agent edit",
      90,
    );
    const appeared = await client.evaluate("!!document.querySelector('.node[data-id=\"remote_spectral\"]')");
    check(appeared, "the remotely added node is missing from the canvas");
    const refreshMs = Date.now() - syncStart;
    const remoteRemove = await control("remove_component", {pipeline_id: pipelineId, node_id: "remote_spectral"});
    check(remoteRemove.success, "remote remove_component failed", JSON.stringify(remoteRemove));
    await until(
      async () => (await client.evaluate("document.querySelectorAll('.node').length")) === knownIds.length,
      "the page did not drop the remotely removed node",
      90,
    );
    report.checks.push({
      name: "agent edits appear in the browser without user action",
      component: "feature.spectral", refresh_ms: refreshMs,
    });

    // An Agent-triggered run must drive the page's status and node badges.
    const remoteRun = await control("execute_pipeline", {pipeline_id: pipelineId});
    check(remoteRun.success, "remote execute_pipeline failed", JSON.stringify(remoteRun));
    let sawRunning = false;
    let badgesAfter = badgesBefore;
    const runDeadline = Date.now() + 180000;
    while (Date.now() < runDeadline) {
      const snapshot = await client.evaluate(`(() => ({
        status: document.querySelector("#pipeline-status").textContent,
        badges: [...document.querySelectorAll(".node-status")].map((el) => el.textContent.trim()),
      }))()`);
      badgesAfter = snapshot.badges;
      if (snapshot.status === "RUNNING" || snapshot.badges.some((badge) => /RUNNING|READY/.test(badge))) {
        sawRunning = true;
      }
      if (["SUCCESS", "FAILED", "CANCELLED"].includes(snapshot.status)) break;
      await pause(100);
    }
    const finalStatus = await client.evaluate("document.querySelector('#pipeline-status').textContent");
    check(finalStatus === "SUCCESS", `agent-triggered run ended with ${finalStatus}`);
    const progressed = badgesAfter.some((badge, index) => badge !== badgesBefore[index]);
    check(progressed, `node badges never updated: ${JSON.stringify(badgesAfter)}`);
    const nodeMetaRendered = await client.evaluate(
      "[...document.querySelectorAll('.node-status')].some((el) => /ms|复用/.test(el.textContent))",
    );
    report.checks.push({
      name: "agent-triggered run shows live progress in the browser",
      status: finalStatus, saw_running: sawRunning, node_meta_rendered: nodeMetaRendered,
      badge_sample: badgesAfter.slice(0, 3),
    });

    await client.evaluate("document.querySelector('#run').click()");
    await until(
      async () => ["SUCCESS", "FAILED", "CANCELLED"].includes(
        await client.evaluate("document.querySelector('#pipeline-status').textContent"),
      ),
      "pipeline did not finish inside the browser",
      600,
    );
    const status = await client.evaluate("document.querySelector('#pipeline-status').textContent");
    if (status !== "SUCCESS") throw new Error(`browser run ended with ${status}`);

    const forestPoint = await client.evaluate(hitPoint("forest"));
    if (forestPoint) await client.click(forestPoint.x, forestPoint.y);
    let resultText = "";
    let caption = "";
    for (let attempt = 0; attempt < 60; attempt++) {
      resultText = await client.evaluate("document.querySelector('#result-content').textContent");
      caption = await client.evaluate("document.querySelector('#result-caption').textContent");
      if (resultText.includes("Accuracy")) break;
      await pause(100);
    }
    if (!resultText.includes("Accuracy")) {
      throw new Error(`metrics did not render; caption=${caption} content=${resultText.slice(0, 300)}`);
    }
    const metrics = await client.evaluate(`(() => { const text = document.querySelector("#result-content").textContent;
      const scores = [...text.matchAll(/(\\d+\\.\\d)%/g)].map((m) => m[0]);
      return {preview: text.slice(0, 240), scores}; })()`);
    if (!metrics.scores.length) throw new Error("metric panel rendered without any percentage");
    const linePoint = await client.evaluate(hitPoint("line"));
    if (linePoint) await client.click(linePoint.x, linePoint.y);
    await until(
      async () => (await client.evaluate("document.querySelectorAll('#result-content svg.chart polyline').length")) > 0,
      "line plot did not render for the visualization node",
    );
    const chartPoints = await client.evaluate("document.querySelector('#result-content svg.chart polyline').getAttribute('points').length");
    if (chartPoints < 20) throw new Error("rendered chart is empty");
    report.checks.push({
      name: "browser run renders metrics and a bounded chart",
      status, metric_scores: metrics.scores, metrics_preview: metrics.preview.replace(/\s+/g, " ").trim(),
      chart_points: chartPoints,
    });
    await client.screenshot(path.join(shootDir, "03-results.png"));

    const historyTab = await client.evaluate(`(() => { document.querySelector('[data-tab="history"]').click(); return true; })()`);
    if (!historyTab) throw new Error("history tab is not clickable");
    await until(
      async () => (await client.evaluate("document.querySelector('#result-content').textContent")).includes("SUCCESS"),
      "execution history did not render",
    );
    const xml = await client.evaluate(`(async () => { document.querySelector('[data-tab="xml"]').click();
      await new Promise((r) => setTimeout(r, 600));
      return document.querySelector("#result-content pre")?.textContent || ""; })()`);
    if (!xml.includes("<faultPredictionPipeline") || !xml.includes(pipelineId)) {
      throw new Error("XML tab did not show the persisted pipeline");
    }
    report.checks.push({name: "history and XML tabs render from the backend", xml_characters: xml.length});

    report.screenshots = fs.readdirSync(shootDir).map((name) => path.join(shootDir, name));
    report.success = true;
  } finally {
    if (client) client.socket.close();
    chrome.kill();
    server.kill();
  }
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(`browser check failed: ${error.message}`);
  process.exit(1);
});
