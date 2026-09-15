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
    "\nwindow.testApp={state,api,init,openGraph,addNode,commit,activeNode,applyParameters,portClick,copySelection,removeSelection,undo,showResult,runPipeline,fitCanvas,autoLayout,renderInspector,renderCatalog};");
  app = window.testApp;
  await until(() => app.state.graph && window.document.querySelectorAll(".component-item").length === 29,
    "UI failed to initialize");
}, {timeout:15000});
after(() => {
  if (dom) dom.window.close();
  if (processHandle) processHandle.kill();
});

test("registry renders catalog and typed parameter forms", async () => {
  assert.equal(window.document.querySelector("#catalog-count").textContent, "29");
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
  assert.equal(window.document.querySelectorAll(".metric-card").length, 5);
  assert.match(window.document.querySelector("#result-content").textContent, /Accuracy/);
  app.state.selected = new Set(["line"]); await app.showResult();
  assert.ok(window.document.querySelector("svg.chart"));
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
