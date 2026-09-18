// DOM integration tests, not a substitute for visual/browser layout acceptance.
const {test, before, after} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const net = require("node:net");
const {spawn} = require("node:child_process");
const {JSDOM} = require("jsdom");
const {webcrypto} = require("node:crypto");

let processHandle, origin, dom, app, window;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, message) {
  for (let i = 0; i < 150; i++) {
    if (await predicate()) return;
    await pause(40);
  }
  throw new Error(message);
}
before(async () => {
  const port = await new Promise(resolve => {
    const server = net.createServer().listen(0, "127.0.0.1", () => {
      const port = server.address().port; server.close(() => resolve(port));
    });
  });
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "fault-ui-tests-"));
  const python = process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python";
  processHandle = spawn(python, ["-m","fault_platform","serve","--port",String(port),
    "--data-root",path.join(folder, "data"), "--storage-root",path.join(folder, "saved")],
    {windowsHide: true, stdio:"ignore"});
  origin = "http://127.0.0.1:" + port;
  await until(async () => {
    try { return (await fetch(origin + "/api/health")).ok; } catch { return false; }
  }, "test API server did not start");
  const html = fs.readFileSync("src/fault_platform/web/index.html", "utf8");
  dom = new JSDOM(html, {url:origin, runScripts:"outside-only", pretendToBeVisual:true});
  window = dom.window;
  window.fetch = (url, options) => fetch(new URL(url, origin), options);
  window.structuredClone = structuredClone;
  window.CSS = {escape: value => value};
  Object.defineProperty(window, "crypto", {value: webcrypto});
  window.eval(fs.readFileSync("src/fault_platform/web/app.js", "utf8") +
    "\nwindow.testApp={state,api,init,openGraph,addNode,commit,activeNode,applyParameters,portClick,copySelection,removeSelection,undo,showResult,orthogonalRoute,edgePathD,claimLane,runPipeline,fitCanvas,autoLayout,renderInspector,renderCatalog,toggleGroup,toggleAllGroups,setPanelSize,panelCeiling,persistLayout,updateSplitterPositions,resetLayout,DEFAULT_LAYOUT,PANEL_LIMITS};");
  app = window.testApp;
  await until(() => app.state.graph && window.document.querySelectorAll(".component-item").length === 88,
    "UI failed to initialize");
}, {timeout:15000});
after(() => {
  if (dom) dom.window.close();
  if (processHandle) processHandle.kill();
});

test("registry renders catalog and typed parameter forms", async () => {
  assert.equal(window.document.querySelector("#catalog-count").textContent, "88");
  await app.addNode("data.input", {x:20,y:20}, {path:"anything.csv"});
  assert.equal(app.activeNode().type, "data.input");
  assert.equal(window.document.querySelector("#parameter-0").value, "anything.csv");
  const field = window.document.querySelector("#parameter-0");
  field.value = "edited.csv";
  field.dispatchEvent(new window.Event("input", {bubbles:true}));
  const version = app.state.graph.version;
  window.document.querySelector("#parameter-form").dispatchEvent(new window.Event("submit", {cancelable:true}));
  await until(() => app.state.graph.version > version, "parameter form did not save");
  assert.equal(app.activeNode().parameters.path, "edited.csv");
});

test("port connections, copy, delete and undo keep server graph consistent", async () => {
  await app.openGraph((await app.api("create_pipeline", {name:"UI connection test"})).graph);
  await app.addNode("data.input", {x:20,y:20}, {path:"edited.csv"});
  const source = app.activeNode().id;
  await app.addNode("data.filter", {x:300,y:20}, {column:"value",value:0});
  const target = app.activeNode().id;
  await app.portClick({node:source,port:"dataset",kind:"output"});
  await app.portClick({node:target,port:"dataset",kind:"input"});
  assert.equal(app.state.graph.edges.length, 1);
  const selected = new Set([source,target]); app.state.selected = selected;
  await app.copySelection();
  assert.equal(app.state.graph.nodes.length, 4);
  assert.equal(app.state.graph.edges.length, 2);
  await app.removeSelection();
  assert.equal(app.state.graph.nodes.length, 2);
  await app.undo();
  assert.equal(app.state.graph.nodes.length, 4);
  await app.undo(true);
  assert.equal(app.state.graph.nodes.length, 2);
  const serverGraph = (await app.api("get_pipeline", {pipeline_id:app.state.graph.id})).graph;
  assert.equal(serverGraph.edges.length, 1);
});

test("example executes and renders actual model metrics and charts", async () => {
  await app.openGraph((await app.api("create_example")).graph);
  await app.runPipeline();
  await until(() => !app.state.running, "UI did not observe run completion");
  assert.equal(window.document.querySelector("#pipeline-status").textContent, "SUCCESS");
  app.state.selected = new Set(["forest"]); await app.showResult();
  // 卡片数量随"该报哪些指标"演进：这里只钉住面板确实画了卡片，具体清单由断言内容决定。
  assert.ok(window.document.querySelectorAll(".metric-card").length >= 5);
  const metricsText = window.document.querySelector("#result-content").textContent;
  assert.match(metricsText, /Accuracy/);
  assert.match(metricsText, /Balanced acc\./);
  assert.match(metricsText, /漏报率/);
  // 正负样本构成是"能不能读这个分数"的前提，必须出现在结果面板里。
  assert.match(metricsText, /正负样本构成/);
  assert.match(metricsText, /训练集/);
  assert.match(metricsText, /测试集/);
  assert.match(metricsText, /占比/);
  // 指标口径必须写在脸上：留出集、宏平均（不加权）、逐类数值 + 整体准确率。
  assert.match(metricsText, /以上均为留出集指标/);
  assert.match(metricsText, /宏平均/);
  assert.match(metricsText, /各类指标（留出集）/);
  assert.match(metricsText, /整体准确率/);
  assert.match(metricsText, /支持数/);
  assert.match(metricsText, /精确率/);
  assert.match(metricsText, /召回率/);
  app.state.selected = new Set(["line"]); await app.showResult();
  assert.ok(window.document.querySelector("svg.chart"));
  // 点含数据的节点时，结果面板要给出"导出 CSV"入口，而且链接真的能下到文件。
  app.state.selected = new Set(["stat"]); await app.showResult();
  const exportBlock = window.document.querySelector(".export-block");
  assert.ok(exportBlock, "节点结果面板应出现导出数据入口");
  const links = [...exportBlock.querySelectorAll("a.export-button")];
  assert.ok(links.length >= 2, "输入侧与输出侧都应可导出");
  for (const link of links) assert.match(link.getAttribute("href"), /\/api\/node-data\/csv\?/);
  const outputLink = links.find((a) => a.closest(".export-row").querySelector(".export-tag.output"));
  assert.ok(outputLink, "输出侧应有导出链接");
  const download = await fetch(new URL(outputLink.getAttribute("href"), origin));
  assert.ok(download.ok, "导出链接应当真的返回文件");
  assert.match(download.headers.get("content-type"), /text\/csv/);
  assert.match(await download.text(), /window_id/);
  // 数据概览也要报正负样本比例（示例图的 overview 配了 label_column）。
  app.state.selected = new Set(["overview"]); await app.showResult();
  const overviewText = window.document.querySelector("#result-content").textContent;
  assert.match(overviewText, /标签构成/);
  assert.match(overviewText, /占比/);
  // 示例数据是三分类：没有唯一"正类"，三类的占比都要画出来（各 33.33%）。
  assert.match(overviewText, /33\.33%/);
  app.state.tab = "history"; await app.showResult();
  assert.match(window.document.querySelector("#result-content").textContent, /SUCCESS/);
  app.state.tab = "xml"; await app.showResult();
  assert.match(window.document.querySelector("#result-content").textContent, /faultPredictionPipeline/);
});

test("malicious XML node label is rendered as text", async () => {
  const candidate = structuredClone(app.state.graph);
  candidate.nodes[0].ui.label = '<img src=x onerror="window.pwned=true">';
  await app.commit(candidate);
  assert.equal(window.document.querySelector("#nodes img"), null);
  assert.equal(window.pwned, undefined);
});

test("component library folds into a directory and stays searchable", async () => {
  const document = window.document;
  const library = document.querySelector("#component-library");
  const collapsedCount = () => library.querySelectorAll(".group-body.collapsed").length;
  // 88 个组件超过自动折叠阈值：默认给目录，但组件节点仍全部留在 DOM 中（拖拽与计数依赖它们）。
  assert.equal(library.querySelectorAll(".component-item").length, 88);
  assert.ok(collapsedCount() > 0, "a large catalog should start with collapsed subgroups");

  const selector = '.subcategory-title[data-group="sub:feature/频域 Frequency"]';
  library.querySelector(selector).click();
  const header = library.querySelector(selector);
  assert.equal(header.getAttribute("aria-expanded"), "true");
  assert.equal(header.nextElementSibling.classList.contains("collapsed"), false);
  assert.match(window.localStorage.getItem("fault-library-expanded") || "", /频域 Frequency/);

  const search = document.querySelector("#search");
  search.value = "频域";
  search.dispatchEvent(new window.Event("input", {bubbles: true}));
  assert.equal(collapsedCount(), 0, "searching must expand the groups that match");
  assert.ok([...library.querySelectorAll(".component-item")].some((item) => item.dataset.type === "feature.spectral"));
  search.value = "";
  search.dispatchEvent(new window.Event("input", {bubbles: true}));

  document.querySelector("#toggle-groups").click();
  assert.equal(library.querySelectorAll(".group-body:not(.collapsed)").length, 0);
  document.querySelector("#toggle-groups").click();
  assert.equal(collapsedCount(), 0);
});

test("panel splitters resize the layout within limits and persist", async () => {
  const style = window.document.documentElement.style;
  app.setPanelSize("library", 320);
  assert.equal(style.getPropertyValue("--library-width"), "320px");
  app.setPanelSize("library", 9999);
  // 上限取"面板硬上限"与"视口 34%"中的较小者，保证画布仍留有空间。
  assert.equal(app.state.layout.library, app.panelCeiling("library"));
  assert.ok(app.state.layout.library <= app.PANEL_LIMITS.library[1]);
  app.setPanelSize("library", 10);
  assert.equal(app.state.layout.library, app.PANEL_LIMITS.library[0]);

  app.setPanelSize("library", 300);
  app.persistLayout();
  assert.match(window.localStorage.getItem("fault-layout") || "", /"library":300/);
  const handle = window.document.querySelector("#library-splitter");
  handle.dispatchEvent(new window.KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true}));
  assert.equal(app.state.layout.library, 316, "ArrowRight widens the left panel");
  handle.dispatchEvent(new window.KeyboardEvent("keydown", {key: "ArrowLeft", bubbles: true}));
  assert.equal(app.state.layout.library, 300);

  app.setPanelSize("inspector", 240);
  assert.equal(style.getPropertyValue("--inspector-width"), "240px");
  app.setPanelSize("results", 300);
  assert.equal(style.getPropertyValue("--results-height"), "300px");

  window.document.querySelector("#reset-layout").click();
  assert.equal(app.state.layout.library, app.DEFAULT_LAYOUT.library);
  assert.equal(app.state.layout.inspector, app.DEFAULT_LAYOUT.inspector);
  assert.equal(app.state.layout.results, app.DEFAULT_LAYOUT.results);

  // 拖拽方向：左栏的分隔条在面板右沿（向右拖=变宽），
  // 右栏与底部面板的分隔条贴的是朝内的那条边（向左拖=右栏变宽，向上拖=结果面板变高）。
  const drag = (selector, from, to) => {
    const handle = window.document.querySelector(selector);
    const send = (type, point) => handle.dispatchEvent(new window.MouseEvent(type,
      {clientX: point.x, clientY: point.y, bubbles: true, cancelable: true}));
    send("pointerdown", from);
    send("pointermove", to);
    send("pointerup", to);
  };
  drag("#library-splitter", {x: 200, y: 300}, {x: 240, y: 300});
  assert.equal(app.state.layout.library, app.DEFAULT_LAYOUT.library + 40, "向右拖放宽左侧组件库");
  drag("#inspector-splitter", {x: 900, y: 300}, {x: 860, y: 300});
  assert.equal(app.state.layout.inspector, app.DEFAULT_LAYOUT.inspector + 40, "向左拖放宽右侧配置面板");
  drag("#results-splitter", {x: 600, y: 500}, {x: 600, y: 460});
  assert.equal(app.state.layout.results, app.DEFAULT_LAYOUT.results + 40, "向上拖抬高底部结果面板");
});

test("connections are routed as rounded orthogonal polylines", async () => {
  await app.openGraph((await app.api("create_example")).graph);
  const paths = [...window.document.querySelectorAll("#connections path[data-edge]")];
  assert.ok(paths.length >= 10, `expected the example edges to be drawn, got ${paths.length}`);
  let rounded = 0;
  for (const path of paths) {
    const d = path.getAttribute("d");
    // 贝塞尔会写成 C 命令；正交折线只允许 M / L / Q（Q 就是折角圆角）。
    assert.ok(!/[CcSsAa]/.test(d), `edge path must not contain curve commands: ${d}`);
    if (/Q/.test(d)) rounded += 1;
  }
  assert.ok(rounded >= 1, "至少要有连线带圆角折点（同一行的两个端口可以是一条直线）");
  assert.equal(window.document.querySelectorAll("#connections path.edge-hit").length, paths.length,
    "every edge needs a transparent click hit path");
  const route = app.orthogonalRoute({x:100, y:100}, {x:500, y:260}, 0);

  const d = app.edgePathD(route);
  assert.equal((d.match(/Q/g) || []).length, 2, "一条常规 Z 形路由有且只有两个圆角");
  assert.ok(!/[CcSsAa]/.test(d), `edge path must stay polygonal: ${d}`);
  // 两个端口正好在同一行时，正确的画法就是一条直线（Simulink 也是这样）
  const straight = app.edgePathD(app.orthogonalRoute({x:100, y:100}, {x:400, y:100}, 0));
  assert.ok(!/Q/.test(straight), `same row ports must stay a straight line: ${straight}`);
  assert.deepEqual(route[0], {x:100, y:100}, "route starts exactly on the output port");
  assert.deepEqual(route[route.length - 1], {x:500, y:260}, "route ends exactly on the input port");
  for (let i = 1; i < route.length; i++) {
    const dx = Math.abs(route[i].x - route[i - 1].x), dy = Math.abs(route[i].y - route[i - 1].y);
    assert.ok(dx < 1e-9 || dy < 1e-9, `segment ${i} is neither horizontal nor vertical`);
  }
  // 回边（目标被拖到左边）同样必须正交，并且要绕行而不是横穿端口行
  const back = app.orthogonalRoute({x:600, y:100}, {x:200, y:300}, 0);
  assert.equal(back[0].x, 600);
  assert.equal(back[back.length - 1].x, 200);
  for (let i = 1; i < back.length; i++) {
    const dx = Math.abs(back[i].x - back[i - 1].x), dy = Math.abs(back[i].y - back[i - 1].y);
    assert.ok(dx < 1e-9 || dy < 1e-9, `feedback segment ${i} is diagonal`);
  }
  // 同一条竖直通道上并行的两条线必须错开，不能重叠成一条
  const occupied = [];
  const first = app.claimLane(occupied, {x:100, y:100}, {x:500, y:400});
  const second = app.claimLane(occupied, {x:100, y:100}, {x:500, y:400});
  assert.notEqual(first, second, "parallel edges in one channel must be spread apart");
});

test("input ports advertise the compatible types they accept", async () => {
  await app.openGraph((await app.api("create_example")).graph);
  // 概览既吃原始表也吃特征表：端口提示与检查器都要写清楚，否则用户不知道能挂哪。
  const port = window.document.querySelector('.node[data-id="overview"] .port.input[data-port="dataset"]');
  assert.match(port.getAttribute("title"), /Dataset \| FeatureDataset/);
  app.state.selected = new Set(["overview"]);
  await app.renderInspector();
  const inspector = window.document.querySelector("#inspector-content").textContent;
  assert.match(inspector, /dataset : Dataset \| FeatureDataset/);
});
