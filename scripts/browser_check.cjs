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

  async screenshot(destination, clip = null, scale = 1) {
    // clip + scale 用来输出"放大图"：整屏截图看不出 1px 分隔条与树形引导线的细节。
    const params = {format: "png"};
    if (clip) params.clip = {...clip, scale};
    const {data} = await this.send("Page.captureScreenshot", params);
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
    if (health.components !== 56) throw new Error(`expected 56 components, got ${health.components}`);
    report.checks.push({name: "server exposes the full registry", components: health.components});

    client = await Client.connect(cdpPort);
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Page.navigate", {url: origin});
    await until(
      () => client.evaluate("document.readyState === 'complete' && document.querySelectorAll('.component-item').length === 56"),
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

    // 组件库折叠：56 个组件超过自动折叠阈值，默认给"目录"，展开与搜索都必须能看到组件。
    const folding = await client.evaluate(`(() => {
      const library = document.querySelector("#component-library");
      const domItems = library.querySelectorAll(".component-item").length;
      const visible = () => [...library.querySelectorAll(".component-item")]
        .filter((item) => item.getBoundingClientRect().height > 0).length;
      const first = {collapsedBodies: library.querySelectorAll(".group-body.collapsed").length,
        visible: visible()};
      document.querySelector('.subcategory-title[data-group="sub:feature/频域 Frequency"]').click();
      const header = library.querySelector('.subcategory-title[data-group="sub:feature/频域 Frequency"]');
      const expanded = {aria: header.getAttribute("aria-expanded"),
        bodyCollapsed: header.nextElementSibling.classList.contains("collapsed"), visible: visible()};
      document.querySelector("#search").value = "频域";
      document.querySelector("#search").dispatchEvent(new Event("input", {bubbles: true}));
      const searching = {collapsedBodies: library.querySelectorAll(".group-body.collapsed").length,
        visible: visible(), count: document.querySelector("#catalog-count").textContent};
      document.querySelector("#search").value = "";
      document.querySelector("#search").dispatchEvent(new Event("input", {bubbles: true}));
      document.querySelector("#toggle-groups").click();
      const collapsed = {collapsedBodies: library.querySelectorAll(".group-body.collapsed").length,
        visible: visible(), fitsWithoutScroll: library.scrollHeight <= library.clientHeight + 4};
      document.querySelector("#toggle-groups").click();
      const headers = [...library.querySelectorAll(".category-title, .subcategory-title")];
      const clipped = headers.filter((header) => header.scrollWidth > header.clientWidth + 1).length;
      return {domItems, first, expanded, searching, collapsed, expandedAgain: visible(), clippedHeaders: clipped};
    })()`);
    if (folding.domItems !== 56) {
      throw new Error(`collapsing must keep all 56 items in the DOM, got ${folding.domItems}`);
    }
    if (!(folding.first.collapsedBodies > 0 && folding.first.visible < 56)) {
      throw new Error(`a large catalog should open as a directory: ${JSON.stringify(folding.first)}`);
    }
    if (folding.expanded.aria !== "true" || folding.expanded.bodyCollapsed) {
      throw new Error(`clicking a subgroup header did not expand it: ${JSON.stringify(folding.expanded)}`);
    }
    if (folding.searching.collapsedBodies !== 0 || folding.searching.visible === 0) {
      throw new Error(`search must reveal matches: ${JSON.stringify(folding.searching)}`);
    }
    if (folding.collapsed.visible !== 0 || folding.expandedAgain < 40) {
      throw new Error(`collapse-all/expand-all failed: ${JSON.stringify(folding)}`);
    }
    if (!folding.collapsed.fitsWithoutScroll || folding.clippedHeaders) {
      throw new Error(`the directory view must fit without scroll or clipping: ${JSON.stringify(folding)}`);
    }
    report.checks.push({name: "component library folds into a directory and stays searchable", ...folding});
    // 此时组件库已全部展开（默认目录视图见 01-catalog.png）。
    await client.screenshot(path.join(shootDir, "01b-library-expanded.png"));
    // 放大图：树形引导线与分隔条在整屏截图里几乎看不见，人工评审需要 3x 裁剪。
    const treeBox = await client.evaluate(`(() => { const rect = document.querySelector(".category-title").getBoundingClientRect();
      return {x: 0, y: Math.max(0, Math.round(rect.top) - 14)}; })()`);
    await client.screenshot(path.join(shootDir, "01c-library-tree-zoom.png"),
      {x: treeBox.x, y: treeBox.y, width: 250, height: 210}, 3);
    const edgeBox = await client.evaluate(`(() => { const rect = document.querySelector("#library-splitter").getBoundingClientRect();
      return {x: Math.max(0, Math.round(rect.left) - 40)}; })()`);
    // 静止时分隔条是透明的，只有悬停才出现抓手，所以这张放大图要在悬停态下截。
    await client.mouse("mouseMoved", edgeBox.x + 43, 400, {buttons: 0});
    await pause(220);
    await client.screenshot(path.join(shootDir, "01d-panel-splitter-zoom.png"),
      {x: edgeBox.x, y: 240, width: 90, height: 320}, 3);
    await client.mouse("mouseMoved", 700, 400, {buttons: 0});
    await pause(120);

    // 面板尺寸：真实指针拖动分隔条 → 宽度变化并写入 localStorage，刷新后仍然生效。
    const beforeResize = await client.evaluate(`(() => { const box = document.querySelector("#component-library").getBoundingClientRect();
      const handle = document.querySelector("#library-splitter").getBoundingClientRect();
      return {width: Math.round(box.width), handle: {x: handle.left + handle.width / 2, y: handle.top + 120}}; })()`);
    if (beforeResize.handle.x < 100) throw new Error("library splitter was not positioned");
    await client.mouse("mousePressed", beforeResize.handle.x, beforeResize.handle.y);
    for (let step = 1; step <= 6; step++) {
      await client.mouse("mouseMoved", beforeResize.handle.x + (90 * step) / 6, beforeResize.handle.y);
    }
    await client.mouse("mouseReleased", beforeResize.handle.x + 90, beforeResize.handle.y, {buttons: 0});
    await pause(200);
    const afterResize = await client.evaluate(`(() => { const box = document.querySelector("#component-library").getBoundingClientRect();
      const stored = JSON.parse(localStorage.getItem("fault-layout") || "{}");
      const canvas = document.querySelector("#canvas").getBoundingClientRect();
      return {width: Math.round(box.width), stored, canvas: [canvas.width, canvas.height],
        variable: document.documentElement.style.getPropertyValue("--library-width")}; })()`);
    if (afterResize.width - beforeResize.width < 60) {
      throw new Error(`dragging the splitter did not widen the library: ${JSON.stringify({beforeResize, afterResize})}`);
    }
    if (afterResize.stored.library !== parseFloat(afterResize.variable)) {
      // 面板宽度：CSS 变量与 localStorage 必须一致（测量宽度会因为边框差一两个像素）。
      throw new Error(`the new width was not persisted: ${JSON.stringify(afterResize)}`);
    }
    if (afterResize.canvas[0] < 100 || afterResize.canvas[1] < 100) {
      throw new Error(`resizing broke the canvas layout: ${JSON.stringify(afterResize.canvas)}`);
    }
    await client.send("Page.reload", {});
    await until(
      () => client.evaluate("document.readyState === 'complete' && document.querySelectorAll('.component-item').length === 56"),
      "palette did not come back after reload",
    );
    const restored = await client.evaluate(`(() => { const box = document.querySelector("#component-library").getBoundingClientRect();
      return Math.round(box.width); })()`);
    if (Math.abs(restored - afterResize.width) > 2) {
      throw new Error(`panel width was not restored after reload: ${restored} vs ${afterResize.width}`);
    }
    report.checks.push({
      name: "panel splitters resize the layout and survive a reload",
      before: beforeResize.width, after: afterResize.width, restored,
      variable: afterResize.variable, canvas: afterResize.canvas,
    });

    // 拖拽方向：左栏的分隔条在面板右沿（向右拖=变宽），右栏与底部面板的分隔条贴的是"朝内"的
    // 那条边（向左拖=右栏变宽，向上拖=结果面板变高）。真实指针事件驱动，三个方向都要对。
    const dirStart = await client.evaluate(`(() => {
      const size = (selector) => { const rect = document.querySelector(selector).getBoundingClientRect();
        return {w: Math.round(rect.width), h: Math.round(rect.height)}; };
      const handle = (selector) => { const rect = document.querySelector(selector).getBoundingClientRect();
        return {x: rect.left + rect.width / 2, y: rect.top + 3}; };
      return {inspector: size(".inspector"), results: size(".results"), canvas: size("#canvas"),
        inspectorHandle: handle("#inspector-splitter"), resultsHandle: handle("#results-splitter")}; })()`);
    await client.mouse("mousePressed", dirStart.inspectorHandle.x, 400);
    for (let step = 1; step <= 5; step++) {
      await client.mouse("mouseMoved", dirStart.inspectorHandle.x - (80 * step) / 5, 400);
    }
    await client.mouse("mouseReleased", dirStart.inspectorHandle.x - 80, 400, {buttons: 0});
    await pause(150);
    const resultsHandle = await client.evaluate(`(() => { const rect = document.querySelector("#results-splitter").getBoundingClientRect();
      return {x: rect.left + rect.width / 2, y: rect.top + 3}; })()`);
    await client.mouse("mousePressed", resultsHandle.x, resultsHandle.y);
    for (let step = 1; step <= 5; step++) {
      await client.mouse("mouseMoved", resultsHandle.x, resultsHandle.y - (140 * step) / 5);
    }
    await client.mouse("mouseReleased", resultsHandle.x, resultsHandle.y - 140, {buttons: 0});
    await pause(150);
    const dirEnd = await client.evaluate(`(() => {
      const size = (selector) => { const rect = document.querySelector(selector).getBoundingClientRect();
        return {w: Math.round(rect.width), h: Math.round(rect.height)}; };
      return {inspector: size(".inspector"), results: size(".results"), canvas: size("#canvas")}; })()`);
    if (dirEnd.inspector.w - dirStart.inspector.w < 50) {
      throw new Error(`dragging the inspector splitter left must widen it: ${JSON.stringify({dirStart, dirEnd})}`);
    }
    if (dirEnd.results.h - dirStart.results.h < 60) {
      throw new Error(`dragging the results splitter up must make it taller: ${JSON.stringify({dirStart, dirEnd})}`);
    }
    if (dirEnd.canvas.w < 300 || dirEnd.canvas.h < 200) {
      throw new Error(`directional resizing squeezed the canvas: ${JSON.stringify(dirEnd.canvas)}`);
    }
    report.checks.push({
      name: "splitters follow the pointer on all three edges",
      inspector: [dirStart.inspector.w, dirEnd.inspector.w],
      results: [dirStart.results.h, dirEnd.results.h], canvas: dirEnd.canvas,
    });

    // 窄窗口下三栏必须都还可用：面板按视口钳制，不出现横向溢出。
    await client.send("Emulation.setDeviceMetricsOverride", {
      width: 960, height: 720, deviceScaleFactor: 1, mobile: false,
    });
    await pause(300);
    const narrow = await client.evaluate(`(() => {
      const box = (selector) => { const rect = document.querySelector(selector).getBoundingClientRect();
        return [Math.round(rect.width), Math.round(rect.height)]; };
      const libraryHandle = document.querySelector("#library-splitter").getBoundingClientRect();
      const libraryBox = document.querySelector(".library").getBoundingClientRect();
      return {library: box(".library"), canvas: box("#canvas"), inspector: box(".inspector"),
        overflowX: document.documentElement.scrollWidth - innerWidth,
        handleAligned: Math.abs((libraryHandle.left + libraryHandle.width / 2) - libraryBox.right) <= 4};
    })()`);
    await client.send("Emulation.clearDeviceMetricsOverride", {});
    await pause(200);
    for (const [name, size] of Object.entries({library: narrow.library, canvas: narrow.canvas,
      inspector: narrow.inspector})) {
      if (size[0] < 100 || size[1] < 100) throw new Error(`${name} collapsed at 960px: ${size}`);
    }
    if (narrow.overflowX > 1) throw new Error(`layout overflows horizontally: ${narrow.overflowX}px`);
    if (!narrow.handleAligned) throw new Error("the splitter drifted away from the panel edge");
    report.checks.push({name: "three columns stay usable and clamped in a narrow window", ...narrow});

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

    // 连线必须是"圆角正交折线"：沿路径抽样，除折角圆角外不能有斜向行程；
    // 端点还要精确落在端口圆心上（用 screenCTM 把用户坐标换算成屏幕坐标再比）。
    const routing = await client.evaluate(`(() => {
      const edges = ${JSON.stringify(created.graph.edges)};
      const dotCenter = (nodeId, port, kind) => {
        const dot = document.querySelector('.node[data-id="' + CSS.escape(nodeId) + '"] .port.' + kind +
          '[data-port="' + CSS.escape(port) + '"] .port-dot');
        const box = dot.getBoundingClientRect();
        return {x: box.left + box.width / 2, y: box.top + box.height / 2};
      };
      let worstDiagonal = 0, worstEndpoint = 0, curves = 0;
      const paths = [...document.querySelectorAll("#connections path[data-edge]")];
      paths.forEach((path) => {
        const d = path.getAttribute("d");
        if (/[CcSsAa]/.test(d)) curves += 1;
        const length = path.getTotalLength();
        let diagonal = 0, previous = path.getPointAtLength(0);
        const step = Math.max(0.5, length / 500);
        for (let at = step; at <= length; at += step) {
          const point = path.getPointAtLength(at);
          const dx = Math.abs(point.x - previous.x), dy = Math.abs(point.y - previous.y);
          if (dx > 0.05 && dy > 0.05) diagonal += Math.hypot(dx, dy);
          previous = point;
        }
        worstDiagonal = Math.max(worstDiagonal, diagonal);
        const matrix = path.getScreenCTM();
        const start = path.getPointAtLength(0).matrixTransform(matrix);
        const end = path.getPointAtLength(length).matrixTransform(matrix);
        const edge = edges[Number(path.dataset.edge)];
        const from = dotCenter(edge.source_node, edge.source_port, "output");
        const to = dotCenter(edge.target_node, edge.target_port, "input");
        worstEndpoint = Math.max(worstEndpoint,
          Math.hypot(start.x - from.x, start.y - from.y), Math.hypot(end.x - to.x, end.y - to.y));
      });
      return {paths: paths.length, curves,
        worstDiagonal: Math.round(worstDiagonal * 10) / 10,
        worstEndpoint: Math.round(worstEndpoint * 100) / 100,
        hitPaths: document.querySelectorAll("#connections path.edge-hit").length};
    })()`);
    // 折角半径 7，一条边最多 4~6 个圆角，斜向行程上限约 25px；贝塞尔曲线会远超这个量级。
    if (routing.curves) throw new Error(`edges still contain curve commands: ${JSON.stringify(routing)}`);
    if (routing.worstDiagonal > 90) throw new Error(`edges are not orthogonal: ${JSON.stringify(routing)}`);
    if (routing.worstEndpoint > 1.5) throw new Error(`edge endpoints miss the port dots: ${JSON.stringify(routing)}`);
    if (routing.hitPaths !== routing.paths) throw new Error(`every edge needs a click hit path: ${JSON.stringify(routing)}`);
    report.checks.push({name: "connections are drawn as rounded orthogonal polylines", ...routing});
    await client.screenshot(path.join(shootDir, "02-example-graph.png"));
    // 放大图：正交折线的折角与端口接合处，供人工目视评审（整屏截图看不清 7px 圆角）。
    const edgeZoom = await client.evaluate(`(() => {
      const boxes = [...document.querySelectorAll("#connections path[data-edge]")].map((path) => path.getBoundingClientRect());
      const left = Math.min(...boxes.map((box) => box.left)), right = Math.max(...boxes.map((box) => box.right));
      const top = Math.min(...boxes.map((box) => box.top)), bottom = Math.max(...boxes.map((box) => box.bottom));
      const width = Math.min(620, right - left + 40), height = Math.min(440, bottom - top + 40);
      const x = Math.max(0, Math.min((left + right) / 2 - width / 2, innerWidth - width));
      const y = Math.max(0, Math.min((top + bottom) / 2 - height / 2, innerHeight - height));
      return {x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height)};
    })()`);
    await client.screenshot(path.join(shootDir, "02b-edges-zoom.png"), edgeZoom, 2);

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

    // 中间产物必须可检验：把概览挂到特征分支上，服务端要接受（输入端口声明了 FeatureDataset），
    // 跑完之后特征表的行列与列名要能直接读出来，浏览器里也要出现这个新节点。
    const controlApi = (operation, payload) => fetch(`${origin}/api/control/${operation}`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    }).then((response) => response.json());
    const currentGraph = (await controlApi("get_pipeline", {pipeline_id: pipelineId})).graph;
    const featureNode = currentGraph.nodes.filter((node) => node.type.startsWith("feature."))
      .find((node) => node.type !== "feature.merge");
    if (!featureNode) throw new Error("the example graph has no feature branch to inspect");
    await controlApi("add_component", {pipeline_id: pipelineId, component_type: "visual.overview",
      node_id: "feature_overview", position: {x: 760, y: 720}});
    const wired = await controlApi("connect_components", {pipeline_id: pipelineId,
      source_node: featureNode.id, source_port: "features", target_node: "feature_overview",
      target_port: "dataset"});
    if (!wired.success) throw new Error(`a feature branch must be inspectable: ${JSON.stringify(wired)}`);
    await controlApi("execute_pipeline", {pipeline_id: pipelineId});
    await until(async () => {
      const status = await controlApi("get_pipeline_status", {pipeline_id: pipelineId});
      return ["SUCCESS", "FAILED", "CANCELLED"].includes(status.status);
    }, "re-run after wiring the overview node never finished");
    const overviewResult = await controlApi("get_node_result", {pipeline_id: pipelineId, node_id: "feature_overview"});
    const overview = overviewResult.outputs?.overview?.value;
    if (!overview?.row_count || !overview.column_names?.length) {
      throw new Error(`overview on a feature branch returned nothing usable: ${JSON.stringify(overviewResult).slice(0, 300)}`);
    }
    await until(
      () => client.evaluate(`[...document.querySelectorAll(".node")].some((node) => node.dataset.id === "feature_overview")`),
      "the new overview node never appeared in the designer",
    );
    // 点开这个节点：结果面板必须把特征表概览画出来（"N 行 × M 列" + 列名），这是工程师真正要看的。
    // 前一项检查停在 XML 标签页，先切回"运行结果"。
    await client.evaluate(`(() => { document.querySelector('[data-tab="result"]').click(); return true; })()`);
    await client.evaluate("document.querySelector('#fit').click()");
    await pause(300);
    const overviewPoint = await client.evaluate(hitPoint("feature_overview"));
    if (!overviewPoint) throw new Error("the overview node is not clickable in the designer");
    await client.click(overviewPoint.x, overviewPoint.y);
    let featurePanel = "";
    for (let attempt = 0; attempt < 60; attempt++) {
      featurePanel = await client.evaluate("document.querySelector('#result-content').textContent");
      if (featurePanel.includes("__mean")) break;
      await pause(120);
    }
    if (!featurePanel.includes("行 ×") || !featurePanel.includes("__mean")) {
      throw new Error(`the designer did not show the feature table summary: ${featurePanel.slice(0, 200)}`);
    }
    await client.screenshot(path.join(shootDir, "04-feature-overview.png"));
    report.checks.push({
      name: "a feature branch can be inspected by an overview node",
      source: `${featureNode.id}.features`, rows: overview.row_count,
      columns: overview.column_names.length, sample_columns: overview.column_names.slice(0, 3),
    });
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
