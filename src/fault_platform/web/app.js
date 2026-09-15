const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clone = (value) => structuredClone(value);
const categories = {
  data: ["数据处理", "▦"], explore: ["数据探索", "◈"], visual: ["数据可视化", "▤"],
  feature: ["特征提取", "ƒ"], validation: ["算法验证", "◇"],
};
const state = {
  catalog: [], graph: null, selected: new Set(), edge: null, pending: null,
  zoom: 1, pan: {x: 40, y: 35}, undo: [], redo: [], statuses: {}, running: false,
  saving: false, tab: "result", onlyFavorites: false, favorites: new Set(),
  drag: null, resultRequest: 0, dirty: false, poll: null,
  events: null, syncing: false, remoteVersion: null, nodeMeta: {},
};
try { state.favorites = new Set(JSON.parse(localStorage.getItem("fault-favorites") || "[]")); } catch {}
const schema = (type) => state.catalog.find((c) => c.component_type === type);
const activeNode = () => state.graph?.nodes.find((n) => n.id === [...state.selected][0]);
const categoryColor = (category) => categories[category] ? category : "data";
let toastTimer;
function toast(message, error = false) {
  $("#toast").textContent = message;
  $("#toast").className = "show" + (error ? " error-toast" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").className = "", error ? 6500 : 3000);
  $("#footer-message").textContent = String(message).slice(0, 110);
}
async function api(operation, args = {}) {
  const response = await fetch("/api/control/" + operation, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(args),
  });
  const result = await response.json();
  if (!result.success) {
    throw new Error(result.errors?.join("\n") || result.summary || result.detail || "操作失败");
  }
  return result;
}
function guard(fn) {
  return (...args) => Promise.resolve().then(() => fn(...args)).catch((e) => toast(e.message, true));
}
async function listPipelines() {
  const value = await api("list_pipelines");
  $("#pipeline-list").innerHTML = value.pipelines.map((p) =>
    '<option value="' + esc(p.id) + '">' + esc(p.name) + "</option>").join("");
  if (state.graph) $("#pipeline-list").value = state.graph.id;
}
async function dataFiles() {
  const value = await (await fetch("/api/data")).json();
  $("#data-files").innerHTML = '<option value="">选择数据源…</option>' + value.datasets.map((d) =>
    '<option value="' + esc(d.path) + '">' + esc(d.path) + "</option>").join("");
}
async function checkpoints() {
  if (!state.graph) return;
  const result = await api("list_checkpoints", {pipeline_id: state.graph.id});
  $("#checkpoint-list").innerHTML = '<option value="">恢复检查点…</option>' + result.checkpoints.map((c) =>
    '<option value="' + esc(c.checkpoint_id) + '">' + esc(new Date(c.timestamp).toLocaleTimeString()) +
    " · " + c.completed_nodes.length + " 节点</option>").join("");
}
async function openGraph(graph, fit = true) {
  clearTimeout(state.poll);
  state.graph = graph; state.selected.clear(); state.edge = null; state.pending = null;
  state.undo = []; state.redo = []; state.statuses = {}; state.dirty = false; state.nodeMeta = {};
  render(); renderInspector();
  if (fit) fitCanvas();
  await Promise.all([listPipelines(), dataFiles(), checkpoints()]);
  const status = await api("get_pipeline_status", {pipeline_id: graph.id});
  setStatus(status);
  if (state.running) pollStatus();
  await showResult();
}
function renderCatalog() {
  const query = $("#search").value.toLowerCase();
  const filtered = state.catalog.filter((c) =>
    (!state.onlyFavorites || state.favorites.has(c.component_type)) &&
    [c.component_type, c.display_name, c.description, ...(c.tags || [])].join(" ").toLowerCase().includes(query));
  $("#catalog-count").textContent = state.catalog.length;
  $("#component-library").innerHTML = Object.entries(categories).map(([category, [title, icon]]) => {
    const components = filtered.filter((c) => c.category === category);
    if (!components.length) return "";
    return '<div class="category-title">' + title + "<span>" + components.length + "</span></div>" +
      components.map((c) => '<div class="component-item" draggable="true" data-type="' + esc(c.component_type) +
        '" style="--category:var(--' + category + ')" title="' + esc(c.description) + '">' +
        '<span class="component-icon">' + icon + "</span><span>" + esc(c.display_name) + "</span>" +
        '<button class="star ' + (state.favorites.has(c.component_type) ? "saved" : "") +
        '" aria-label="收藏 ' + esc(c.display_name) + '">☆</button></div>').join("");
  }).join("");
  // Unknown plugin categories still get a discoverable group.
  const extras = filtered.filter((c) => !categories[c.category]);
  for (const c of extras) {
    const item = document.createElement("div");
    item.className = "component-item"; item.draggable = true; item.dataset.type = c.component_type;
    item.textContent = c.display_name;
    $("#component-library").append(item);
  }
  for (const item of document.querySelectorAll(".component-item")) {
    item.ondragstart = (e) => e.dataTransfer.setData("component", item.dataset.type);
    item.ondblclick = guard(() => addNode(item.dataset.type));
    const star = item.querySelector(".star");
    if (star) star.onclick = (e) => {
      e.stopPropagation();
      const type = item.dataset.type;
      if (state.favorites.has(type)) state.favorites.delete(type); else state.favorites.add(type);
      localStorage.setItem("fault-favorites", JSON.stringify([...state.favorites]));
      renderCatalog();
    };
  }
}
function applyTransform() {
  $("#canvas-world").style.transform = "translate(" + state.pan.x + "px," + state.pan.y + "px) scale(" + state.zoom + ")";
  $("#zoom-label").textContent = Math.round(state.zoom * 100) + "%";
}
function render() {
  if (!state.graph) return;
  $("#pipeline-name").value = state.graph.name;
  $("#graph-count").textContent = state.graph.nodes.length + " 个节点 · " + state.graph.edges.length + " 条连接";
  $("#empty-canvas").classList.toggle("hidden", state.graph.nodes.length > 0);
  $("#undo").disabled = !state.undo.length || state.running || state.saving;
  $("#redo").disabled = !state.redo.length || state.running || state.saving;
  renderNodes(); applyTransform();
}
function nodeStatus(nodeId) {
  return state.statuses[nodeId] || "PENDING";
}
function renderNodes() {
  $("#nodes").innerHTML = state.graph.nodes.map((n) => {
    const c = schema(n.type); if (!c) return "";
    const status = nodeStatus(n.id);
    const ports = (kind, specs) => specs.map((p) =>
      '<div class="port ' + kind + (state.pending?.node === n.id && state.pending?.port === p.name ? " active" : "") +
      '" data-node="' + esc(n.id) + '" data-port="' + esc(p.name) + '" data-kind="' + kind +
      '" title="' + esc(p.data_type + (p.required ? " · required" : " · optional")) + '">' +
      (kind === "input" ? '<span class="port-dot"></span>' : "") + esc(p.name) +
      (kind === "output" ? '<span class="port-dot"></span>' : "") + "</div>").join("");
    return '<article class="node ' + (state.selected.has(n.id) ? "selected" : "") + '" data-id="' + esc(n.id) +
      '" style="left:' + n.position.x + "px;top:" + n.position.y + "px;--category:var(--" +
      categoryColor(c.category) + ')"><div class="node-title"><span class="component-icon">' +
      (categories[c.category]?.[1] || "◇") + "</span><div>" + esc(n.ui?.label || c.display_name) +
      "<small>" + esc(n.id) + '</small></div></div><div class="ports">' +
      ports("input", c.input_ports) + ports("output", c.output_ports) +
      '</div><div class="node-status ' + status + '">' + esc(statusLabel(n.id, status)) + "</div></article>";
  }).join("");
  document.querySelectorAll(".port").forEach((p) => p.onclick = guard((e) => {
    e.stopPropagation(); return portClick(p.dataset);
  }));
  drawEdges();
}
function portPosition(nodeId, port, kind) {
  const element = document.querySelector('.node[data-id="' + CSS.escape(nodeId) + '"] .port.' + kind +
    '[data-port="' + CSS.escape(port) + '"] .port-dot');
  if (element) {
    const r = element.getBoundingClientRect(), canvas = $("#canvas").getBoundingClientRect();
    return {x:(r.left + r.width / 2 - canvas.left - state.pan.x) / state.zoom,
            y:(r.top + r.height / 2 - canvas.top - state.pan.y) / state.zoom};
  }
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  const c = schema(node.type);
  const idx = (kind === "input" ? c.input_ports : c.output_ports).findIndex((p) => p.name === port);
  const offset = kind === "output" ? c.input_ports.length : 0;
  return {x: node.position.x + (kind === "input" ? 12 : 206),
          y: node.position.y + 63 + (offset + idx) * 22};
}
function curve(a, b) {
  const dx = Math.max(60, Math.abs(b.x - a.x) * .48);
  return "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " " +
    (b.x - dx) + "," + b.y + " " + b.x + "," + b.y;
}
function drawEdges() {
  if (!state.graph) return;
  $("#connections").innerHTML = state.graph.edges.map((e, index) => {
    const a = portPosition(e.source_node, e.source_port, "output");
    const b = portPosition(e.target_node, e.target_port, "input");
    return '<path data-edge="' + index + '" class="' + (state.edge === index ? "selected" : "") +
      '" d="' + curve(a, b) + '"><title>' + esc(e.source_node + "." + e.source_port + " → " +
      e.target_node + "." + e.target_port) + "</title></path>";
  }).join("");
  if (state.pending?.point) {
    const a = portPosition(state.pending.node, state.pending.port, "output");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", "pending"); path.setAttribute("d", curve(a, state.pending.point));
    $("#connections").append(path);
  }
  $("#connections").querySelectorAll("[data-edge]").forEach((path) => path.onclick = (e) => {
    e.stopPropagation(); state.edge = Number(path.dataset.edge); state.selected.clear();
    renderNodes(); renderInspector(); showResult();
  });
}
async function portClick({node, port, kind}) {
  if (state.running || state.saving) return;
  if (kind === "output") {
    state.pending = {node, port}; renderNodes(); toast("点击目标组件的输入端口完成连接"); return;
  }
  if (!state.pending) { toast("先点击一个输出端口"); return; }
  const from = state.pending; state.pending = null;
  const candidate = clone(state.graph);
  candidate.edges.push({source_node: from.node, source_port: from.port, target_node: node, target_port: port});
  await commit(candidate);
}
async function commit(candidate, before = null, history = true) {
  if (state.running) throw new Error("方案运行中，请等待执行结束");
  if (state.saving) throw new Error("正在保存，请稍后再操作");
  const previous = before || clone(state.graph);
  state.saving = true;
  try {
    const result = await api("replace_pipeline", {pipeline_id: state.graph.id, graph: candidate,
                                                expected_version: previous.version});
    if (history) { state.undo.push(previous); if (state.undo.length > 60) state.undo.shift(); state.redo = []; }
    state.graph = result.graph;
    state.statuses = {};
    setStatus({status: "CREATED", node_status: {}});
    state.dirty = false;
  } catch (error) {
    state.graph = previous;
    renderNodes();
    if (String(error.message).includes("another client")) showRemoteBanner(previous.version);
    throw error;
  } finally {
    state.saving = false; render(); renderInspector();
  }
}
async function addNode(type, position, parameters = {}) {
  if (!state.graph) return;
  const c = schema(type);
  const candidate = clone(state.graph);
  const rect = $("#canvas").getBoundingClientRect();
  const pos = position || {x: (rect.width / 2 - state.pan.x) / state.zoom - 109,
                          y: (rect.height / 2 - state.pan.y) / state.zoom - 65};
  const id = "node_" + crypto.randomUUID().replaceAll("-", "").slice(0, 10);
  candidate.nodes.push({id, type, position: pos, ui: {}, parameters: {
    ...Object.fromEntries(c.parameter_schema.map((p) => [p.name, clone(p.default)])), ...parameters,
  }});
  state.selected = new Set([id]); await commit(candidate);
}
function renderInspector() {
  const n = activeNode();
  if (!n) {
    $("#inspector-content").innerHTML = '<div class="inspector-empty"><span>⌖</span><h3>' +
      (state.edge !== null ? "已选择连接" : "选择一个组件") + "</h3><p>" +
      (state.edge !== null ? "按 Delete 或点击工具栏删除连接。" : "查看端口信息并配置运行参数。") + "</p></div>";
    return;
  }
  const c = schema(n.type);
  $("#inspector-content").innerHTML =
    '<div class="inspect-title"><span class="component-icon" style="--category:var(--' +
    categoryColor(c.category) + ')">' + (categories[c.category]?.[1] || "◇") + "</span>" +
    esc(c.display_name) + '</div><p class="inspect-description">' + esc(c.description) +
    '</p><div class="port-info">' + esc(n.id) + " · v" + esc(c.version) +
    '</div><div class="inspect-section">参数设置</div><form id="parameter-form">' +
    c.parameter_schema.map((p, i) => parameterField(p, n.parameters[p.name], i)).join("") +
    '<button type="submit" id="apply-params" class="primary">应用参数</button></form>' +
    '<div class="inspect-section">输入 / 输出端口</div>' +
    [...c.input_ports.map((p) => "↳ " + p.name + " : " + p.data_type),
     ...c.output_ports.map((p) => "↗ " + p.name + " : " + p.data_type)].map((s) =>
      '<div class="port-info">' + esc(s) + "</div>").join("") +
    '<div class="node-actions"><button id="run-node">运行此节点</button><button id="run-from">从此向后执行</button><button id="retry-node">重试此分支</button></div>';
  const form = $("#parameter-form");
  form.oninput = () => { state.dirty = true; };
  form.onsubmit = (e) => {
    e.preventDefault();
    guard(async () => { await applyParameters(); toast("参数已更新"); })();
  };
  $("#run-node").onclick = guard(() => runPipeline("node"));
  $("#run-from").onclick = guard(() => runPipeline("from"));
  $("#retry-node").onclick = guard(() => runPipeline("from"));
  form.querySelectorAll("input,textarea,select,button").forEach((el) => el.disabled = state.running);
  document.querySelectorAll(".node-actions button").forEach((el) => el.disabled = state.running);
}
function parameterField(p, value, index) {
  const id = "parameter-" + index;
  const title = '<span class="parameter-label">' + esc(p.display_name || p.name) +
    (p.required ? "<em>必填</em>" : "") + "</span>";
  let field;
  if (p.type === "boolean") {
    field = '<input id="' + id + '" type="checkbox" ' + (value ? "checked" : "") + ">";
  } else if (p.type === "enum" && !p.allow_multiple) {
    const optional = !p.required ? '<option value="">默认 / 无</option>' : "";
    field = '<select id="' + id + '">' + optional + p.options.map((v) =>
      '<option value="' + esc(v) + '" ' + (v === value ? "selected" : "") + ">" + esc(v) + "</option>").join("") + "</select>";
  } else if (["list", "object", "any"].includes(p.type)) {
    field = '<textarea id="' + id + '" spellcheck="false" placeholder="JSON">' +
      esc(value === null ? "" : JSON.stringify(value)) + "</textarea>";
  } else {
    const numeric = ["integer", "float"].includes(p.type);
    const display = Array.isArray(value) ? value.join(", ") : value ?? "";
    field = '<input id="' + id + '" type="' + (numeric ? "number" : "text") + '" value="' + esc(display) + '"' +
      (numeric ? ' step="' + (p.type === "integer" ? "1" : "any") + '"' : "") +
      (p.min !== null ? ' min="' + p.min + '"' : "") +
      (p.max !== null ? ' max="' + p.max + '"' : "") +
      (["column_list", "feature_list"].includes(p.type) ? ' placeholder="列名用逗号分隔"' : "") + ">";
  }
  return '<label class="parameter" for="' + id + '">' + title + field +
    (p.description ? "<small>" + esc(p.description) + "</small>" : "") + "</label>";
}
async function applyParameters() {
  const node = activeNode(); if (!node) return;
  const c = schema(node.type), parameters = {};
  for (const [i, p] of c.parameter_schema.entries()) {
    const field = $("#parameter-" + i);
    if (!field) continue;
    const raw = field.value.trim();
    if (p.type === "boolean") parameters[p.name] = field.checked;
    else if (["column_list", "feature_list"].includes(p.type) || p.allow_multiple) parameters[p.name] = raw ? raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [];
    else if (["integer", "float"].includes(p.type)) parameters[p.name] = raw ? Number(raw) : null;
    else if (["list", "object", "any"].includes(p.type)) {
      try { parameters[p.name] = raw ? JSON.parse(raw) : null; }
      catch { throw new Error(p.name + " 需要合法 JSON，例如 0、[1,2] 或 \"文本\""); }
    } else parameters[p.name] = raw || (p.default === null ? null : "");
  }
  const candidate = clone(state.graph);
  candidate.nodes.find((n) => n.id === node.id).parameters = parameters;
  await commit(candidate);
}
async function removeSelection() {
  const candidate = clone(state.graph);
  if (state.edge !== null) {
    candidate.edges.splice(state.edge, 1); state.edge = null;
  } else {
    candidate.nodes = candidate.nodes.filter((n) => !state.selected.has(n.id));
    candidate.edges = candidate.edges.filter((e) => !state.selected.has(e.source_node) && !state.selected.has(e.target_node));
    state.selected.clear();
  }
  await commit(candidate);
}
async function copySelection() {
  const candidate = clone(state.graph), mapping = new Map();
  for (const node of state.graph.nodes.filter((n) => state.selected.has(n.id))) {
    const copy = clone(node); copy.id = "node_" + crypto.randomUUID().replaceAll("-", "").slice(0, 10);
    copy.position.x += 35; copy.position.y += 40; mapping.set(node.id, copy.id); candidate.nodes.push(copy);
  }
  for (const edge of state.graph.edges) {
    if (mapping.has(edge.source_node) && mapping.has(edge.target_node))
      candidate.edges.push({...edge, source_node: mapping.get(edge.source_node), target_node: mapping.get(edge.target_node)});
  }
  state.selected = new Set(mapping.values()); await commit(candidate);
}
async function undo(redo = false) {
  const from = redo ? state.redo : state.undo, to = redo ? state.undo : state.redo;
  if (!from.length) return;
  const current = clone(state.graph), candidate = clone(from[from.length - 1]);
  await commit(candidate, current, false);
  from.pop(); to.push(current); render();
}
function point(event) {
  const rect = $("#canvas").getBoundingClientRect();
  return {x: (event.clientX - rect.left - state.pan.x) / state.zoom,
          y: (event.clientY - rect.top - state.pan.y) / state.zoom};
}
$("#canvas").onpointerdown = (e) => {
  if (e.button !== 0 || e.target.closest("button,.port,path,.zoom-controls")) return;
  const node = e.target.closest(".node");
  if (node) {
    const id = node.dataset.id;
    if (e.shiftKey) {
      if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    } else if (!state.selected.has(id)) state.selected = new Set([id]);
    state.edge = null; state.dirty = false;
    if (!state.running && !state.saving) {
      state.drag = {kind: "nodes", x: e.clientX, y: e.clientY, before: clone(state.graph), moved: false};
    }
    renderNodes(); renderInspector(); guard(showResult)();
  } else {
    if (state.pending) { state.pending = null; drawEdges(); }
    state.drag = {kind: "pan", x: e.clientX, y: e.clientY, origin: {...state.pan}, moved: false};
    state.selected.clear(); state.edge = null; renderNodes(); renderInspector();
  }
  e.preventDefault();
};
window.addEventListener("pointermove", (e) => {
  if (state.pending) { state.pending.point = point(e); drawEdges(); }
  const drag = state.drag; if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  if (drag.kind === "pan") {
    state.pan = {x: drag.origin.x + dx, y: drag.origin.y + dy}; applyTransform();
  } else {
    for (const n of state.graph.nodes) {
      if (state.selected.has(n.id)) {
        const initial = drag.before.nodes.find((old) => old.id === n.id);
        n.position = {x: Math.max(0, initial.position.x + dx / state.zoom),
                      y: Math.max(0, initial.position.y + dy / state.zoom)};
      }
    }
    renderNodes();
  }
});
window.addEventListener("pointerup", guard(async () => {
  const drag = state.drag; state.drag = null;
  if (drag?.kind === "nodes" && drag.moved) await commit(clone(state.graph), drag.before);
}));
$("#canvas").ondragover = (e) => e.preventDefault();
$("#canvas").ondrop = guard(async (e) => {
  e.preventDefault(); const type = e.dataTransfer.getData("component");
  if (type) await addNode(type, point(e));
});
function zoom(factor, center) {
  const rect = $("#canvas").getBoundingClientRect();
  const p = center || {x: rect.width / 2, y: rect.height / 2};
  const next = Math.max(.2, Math.min(2, state.zoom * factor));
  state.pan = {x: p.x - (p.x - state.pan.x) * next / state.zoom,
               y: p.y - (p.y - state.pan.y) * next / state.zoom};
  state.zoom = next; applyTransform();
}
$("#canvas").addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = $("#canvas").getBoundingClientRect();
  zoom(e.deltaY < 0 ? 1.08 : .92, {x: e.clientX - rect.left, y: e.clientY - rect.top});
}, {passive: false});
function fitCanvas() {
  const nodes = state.graph?.nodes; if (!nodes?.length) { state.zoom = 1; state.pan = {x: 40, y: 35}; applyTransform(); return; }
  const rect = $("#canvas").getBoundingClientRect();
  const left = Math.min(...nodes.map((n) => n.position.x)), top = Math.min(...nodes.map((n) => n.position.y));
  const right = Math.max(...nodes.map((n) => n.position.x + 218));
  const bottom = Math.max(...nodes.map((n) => {
    const c = schema(n.type); return n.position.y + 87 + 22 * (c.input_ports.length + c.output_ports.length);
  }));
  state.zoom = Math.max(.2, Math.min(1.1, (rect.width - 65) / (right - left), (rect.height - 65) / (bottom - top)));
  state.pan = {x: (rect.width - (right - left) * state.zoom) / 2 - left * state.zoom,
               y: (rect.height - (bottom - top) * state.zoom) / 2 - top * state.zoom};
  applyTransform();
}
async function autoLayout() {
  const candidate = clone(state.graph);
  const indegree = Object.fromEntries(candidate.nodes.map((n) => [n.id, 0]));
  const level = Object.fromEntries(candidate.nodes.map((n) => [n.id, 0]));
  candidate.edges.forEach((e) => indegree[e.target_node]++);
  const queue = candidate.nodes.filter((n) => !indegree[n.id]).map((n) => n.id);
  for (let i = 0; i < queue.length; i++) {
    for (const e of candidate.edges.filter((e) => e.source_node === queue[i])) {
      level[e.target_node] = Math.max(level[e.target_node], level[e.source_node] + 1);
      if (--indegree[e.target_node] === 0) queue.push(e.target_node);
    }
  }
  const occupied = {};
  for (const id of queue) {
    const n = candidate.nodes.find((n) => n.id === id), x = level[id], c = schema(n.type);
    n.position = {x: 60 + x * 290, y: 40 + (occupied[x] || 0)};
    occupied[x] = (occupied[x] || 0) + 125 + 22 * (c.input_ports.length + c.output_ports.length);
  }
  await commit(candidate); fitCanvas();
}
function setStatus(result) {
  state.running = ["RUNNING", "VALIDATING"].includes(result.status);
  state.statuses = result.node_status || {};
  if (result.status === "CREATED") state.statuses = {};
  $("#pipeline-status").textContent = result.status;
  $("#pipeline-status").className = "status-chip " + result.status;
  $("#run").disabled = state.running;
  $("#cancel").classList.toggle("hidden", !state.running);
  $("#pipeline-name").disabled = state.running;
  for (const id of ["example","new","import","layout","delete","copy","checkpoint"]) $("#" + id).disabled = state.running;
  renderNodes();
}

function statusLabel(nodeId, status) {
  const meta = state.nodeMeta[nodeId];
  if (!meta) return status;
  if (meta.cached) return status + " · 复用";
  return meta.seconds ? status + " · " + (meta.seconds * 1000).toFixed(0) + " ms" : status;
}

function showRemoteBanner(version) {
  state.remoteVersion = version;
  $("#remote-text").textContent = "方案已被其他端修改（v" + version + "）。重新加载会丢弃你未保存的改动。";
  $("#remote-banner").classList.remove("hidden");
}
function hideRemoteBanner() {
  state.remoteVersion = null;
  $("#remote-banner").classList.add("hidden");
}
async function reloadFromServer() {
  if (!state.graph || state.syncing) return;
  state.syncing = true;
  try {
    const graph = (await api("get_pipeline", {pipeline_id: state.graph.id})).graph;
    await openGraph(graph, false);
    hideRemoteBanner();
    toast("已同步其他端的修改（v" + graph.version + "）");
  } finally {
    state.syncing = false;
  }
}
function handleEvent(payload) {
  if (!payload?.type) return;
  const samePipeline = state.graph && payload.pipeline_id === state.graph.id;
  if (payload.type === "graph_changed") {
    if (payload.operation === "create_pipeline" || payload.operation === "load_pipeline") listPipelines();
    if (!samePipeline || payload.version === state.graph.version) return;
    if (state.dirty || state.saving || state.running || state.syncing) { showRemoteBanner(payload.version); return; }
    guard(reloadFromServer)();
    return;
  }
  if (!samePipeline) return;
  if (payload.type === "run_started") {
    setStatus({status: "RUNNING", node_status: state.statuses});
    toast("方案开始执行（" + payload.mode + "），节点状态将实时更新");
  } else if (payload.type === "node_status") {
    state.statuses[payload.node_id] = payload.status;
    renderNodes();
  } else if (payload.type === "history") {
    state.nodeMeta[payload.node_id] = {seconds: payload.execution_time, cached: Boolean(payload.cached)};
    renderNodes();
  } else if (payload.type === "pipeline_status") {
    if (payload.node_status) state.statuses = {...state.statuses, ...payload.node_status};
    setStatus({status: payload.status, node_status: state.statuses});
    render();
    if (["SUCCESS", "FAILED", "CANCELLED"].includes(payload.status)) {
      renderInspector();
      guard(showResult)();
      toast(payload.status === "SUCCESS" ? "方案执行完成，可查看每个节点的中间结果" :
        "执行结束：" + payload.status + (payload.errors?.length ? " · 查看执行日志了解详情" : ""),
        payload.status === "FAILED");
    }
  } else if (payload.type === "checkpoint") {
    checkpoints();
  }
}
function subscribeEvents() {
  if (typeof EventSource !== "function") return;
  if (state.events) state.events.close();
  const source = new EventSource("/api/events");
  source.onmessage = (message) => {
    try { handleEvent(JSON.parse(message.data)); } catch {}
  };
  state.events = source;
}
async function runPipeline(mode = "all") {
  if (state.dirty) await applyParameters();
  const args = {pipeline_id: state.graph.id, mode, incremental: mode === "all"};
  if (mode !== "all") { if (!activeNode()) throw new Error("请先选择节点"); args.node_id = activeNode().id; }
  const result = await api("execute_pipeline", args);
  setStatus({...result, node_status: state.statuses}); renderInspector();
  toast("开始运行，节点状态将自动更新"); pollStatus();
}
async function pollStatus() {
  try {
    const pipeline = state.graph.id;
    const response = await fetch("/api/control/get_pipeline_status", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({pipeline_id: pipeline}),
    });
    const result = await response.json();
    if (state.graph.id !== pipeline) return;
    setStatus(result);
    if (state.running) { state.poll = setTimeout(pollStatus, 500); return; }
    if (!state.selected.size && state.graph.nodes.some((n) => n.id === "compare")) state.selected.add("compare");
    render(); renderInspector(); await showResult();
    toast(result.status === "SUCCESS" ? "方案运行完成，可查看每个节点的中间结果" :
      "执行结束：" + result.status + (result.errors ? " · 查看执行日志了解详情" : ""), result.status === "FAILED");
  } catch (e) {
    toast(e.message, true); state.poll = setTimeout(pollStatus, 1500);
  }
}
function table(rows, columns) {
  if (!rows?.length) return '<p class="metric-note">没有数据行。</p>';
  const cols = columns || Object.keys(rows[0]);
  const format = (v) => typeof v === "number" ? (Number.isInteger(v) ? v : Number(v.toPrecision(6))) :
    typeof v === "object" && v !== null ? JSON.stringify(v) : v ?? "—";
  return '<div class="table-wrap"><table><thead><tr>' + cols.map((c) => "<th>" + esc(c) + "</th>").join("") +
    "</tr></thead><tbody>" + rows.map((r) => "<tr>" + cols.map((c) => "<td>" + esc(format(r[c])) +
      "</td>").join("") + "</tr>").join("") + "</tbody></table></div>";
}
function metricsView(m) {
  const keys = [["accuracy","Accuracy"],["precision","Precision"],["recall","Recall"],["f1","F1 score"],["roc_auc","ROC-AUC"]];
  return '<div class="metric-grid">' + keys.map(([k, title]) => '<div class="metric-card"><span class="label">' +
    title + "</span><strong>" + (typeof m[k] === "number" ? (m[k] * 100).toFixed(1) + "%" : "—") +
    "</strong></div>").join("") + '</div><p class="metric-note">' + esc(m.algorithm) + " · " + esc(m.split_method) +
    " · 训练 " + m.train_count + " / 测试 " + m.test_count + "</p>" +
    (m.warnings || []).map((w) => '<div class="warning">' + esc(w) + "</div>").join("") +
    (m.confusion_matrix ? table(m.confusion_matrix.map((row, i) =>
      Object.fromEntries([["实际 / 预测", m.classes[i]], ...row.map((v, j) => [String(m.classes[j]), v])]))): "");
}
function chart(spec) {
  const width = 660, height = 190, colors = ["#527edf", "#21a992", "#d5a353", "#ad7ecb", "#e08585"];
  const series = (spec.series || []).slice(0, 12);
  const points = series.map((s) => s.x.map((x, i) => x == null || s.y[i] == null ? [NaN, NaN] :
    [Number.isFinite(Number(x)) ? Number(x) : Date.parse(x), Number(s.y[i])])
    .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y)));
  const all = points.flat();
  if (!all.length) return '<p class="metric-note">无可绘制的数值点。</p>';
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
  const px = (x) => 48 + (x - xmin) / (xmax - xmin || 1) * (width - 70);
  const py = (y) => height - 30 - (y - ymin) / (ymax - ymin || 1) * (height - 50);
  let body = "";
  for (let i = 0; i < 4; i++) {
    const y = ymin + (ymax - ymin) * i / 3;
    body += '<line x1="48" y1="' + py(y) + '" x2="640" y2="' + py(y) + '" stroke="#edf1f7"/>' +
      '<text x="40" y="' + (py(y) + 3) + '" text-anchor="end">' + Number(y.toPrecision(3)) + "</text>";
  }
  for (const [i, list] of points.entries()) {
    const color = colors[i % colors.length];
    if (spec.kind === "scatter") body += list.map(([x,y]) => '<circle cx="' + px(x) + '" cy="' + py(y) +
      '" r="2.3" fill="' + color + '" opacity=".7"/>').join("");
    else body += '<polyline points="' + list.map(([x,y]) => px(x) + "," + py(y)).join(" ") +
      '" fill="none" stroke="' + color + '" stroke-width="1.5" opacity=".85"/>';
  }
  body += '<text x="350" y="183" text-anchor="middle">' + esc(spec.x_label) + "</text>";
  return '<svg class="chart" viewBox="0 0 660 190" role="img" aria-label="' + esc(spec.title) + '">' + body + "</svg>" +
    '<div class="legend">' + series.map((s,i) => '<span style="--color:' + colors[i % colors.length] + '">' + esc(s.name) +
      "</span>").join("") + "</div>" + (spec.sampled || spec.truncated_groups ? '<p class="metric-note">显示抽样数据 / 最多 12 个分组。</p>' : "");
}
function objectView(value) {
  if (!value || typeof value !== "object") return "<pre>" + esc(JSON.stringify(value, null, 2)) + "</pre>";
  if (value.algorithm && "accuracy" in value) return metricsView(value);
  if (value.series) return chart(value);
  if (value.rows) return table(value.rows);
  if (value.kind === "overview") {
    return '<p class="metric-note">' + value.row_count + " 行 × " + value.column_count + " 列 · " +
      (value.memory_usage / 1024).toFixed(1) + " KB</p>" +
      table(value.column_names.filter((name) => typeof name === "string").map((name) => ({
        column: name, dtype: value.data_types[name], missing_rate: value.missing_rate[name],
        unique_count: value.unique_count[name],
      })));
  }
  if (value.values) return table(Object.entries(value.values).map(([column, v]) => ({column, value: v})));
  return "<pre>" + esc(JSON.stringify(value, null, 2)) + "</pre>";
}
function correlationView(result) {
  const labels = result.columns, rows = result.preview;
  return '<div class="table-wrap"><table><thead><tr><th>Correlation</th>' +
    labels.map((label) => "<th>" + esc(label) + "</th>").join("") + "</tr></thead><tbody>" +
    rows.map((row,i) => "<tr><th>" + esc(result.index[i]) + "</th>" + labels.map((label) => {
      const value = row[label];
      const alpha = value == null ? 0 : Math.min(1, Math.abs(value)) * .3;
      const color = value >= 0 ? "71,112,209" : "219,119,101";
      return '<td style="background:rgba(' + color + "," + alpha + ')">' +
        (value == null ? "—" : value.toFixed(3)) + "</td>";
    }).join("") + "</tr>").join("") + "</tbody></table></div>";
}
async function showResult() {
  const request = ++state.resultRequest;
  if (!state.graph) return;
  document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  const node = activeNode();
  $("#result-caption").textContent = node ? (schema(node.type).display_name + " · " + node.id) : "选择节点查看数据与结果";
  let html;
  try {
    if (state.tab === "xml") {
      const r = await api("get_pipeline_xml", {pipeline_id: state.graph.id});
      html = "<pre>" + esc(r.xml) + "</pre>";
    } else if (state.tab === "history") {
      const r = await api("get_history", {pipeline_id: state.graph.id});
      html = table(r.history.map((h) => ({
        节点: h.node_id, 状态: h.state_after, 耗时: (h.execution_time * 1000).toFixed(1) + " ms",
        缓存: h.cached ? "复用" : "—", 错误: h.error?.error_message || "",
      })));
    } else if (node) {
      // A failed node is still a useful result; inspect the observation even on HTTP 400.
      const response = await fetch("/api/control/get_node_result", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({pipeline_id:state.graph.id,node_id:node.id,limit:20})});
      const r = await response.json();
      if (!r.outputs) throw new Error(r.summary || "尚无运行结果");
      html = (r.error ? '<div class="error">' + esc(r.error.error_type + ": " + r.error.error_message) + "</div>" : "") +
        Object.entries(r.outputs).map(([port, result]) => {
          let content;
          if (result.kind === "table") content = '<p class="metric-note">' + result.shape.join(" × ") +
            (result.truncated ? " · 仅显示前 20 行 / 50 列" : "") + "</p>" +
            (schema(node.type).output_ports.find((p) => p.name === port)?.data_type === "CorrelationMatrix" ?
              correlationView(result) : table(result.preview, result.columns));
          else if (result.kind === "object") content = objectView(result.value);
          else if (result.kind === "model") content = '<p class="metric-note">已训练模型：' + esc(result.class) +
            " · " + result.features.length + " 个特征</p>";
          else content = "<pre>" + esc(JSON.stringify(result.preview ?? result, null, 2)) + "</pre>";
          return '<section class="result-port"><h3>' + esc(port) + "</h3>" + content + "</section>";
        }).join("");
      if (!html) html = '<div class="empty-result">此节点尚无有效输出。配置参数后运行方案。</div>';
    } else html = '<div class="empty-result"><span>◈</span>点击节点查看中间数据、特征、图形与模型指标。</div>';
  } catch (error) {
    html = '<div class="empty-result">' + esc(error.message.includes("not been executed") ? "方案尚未执行。" : error.message) + "</div>";
  }
  if (request === state.resultRequest) $("#result-content").innerHTML = html;
}

$("#example").onclick = $("#empty-example").onclick = guard(async () => {
  const result = await api("create_example"); await openGraph(result.graph);
  toast("已加载合成设备示例，点击「运行方案」完成特征与模型验证");
});
$("#new").onclick = guard(async () => openGraph((await api("create_pipeline")).graph));
$("#pipeline-list").onchange = guard(async (e) => openGraph((await api("get_pipeline", {pipeline_id:e.target.value})).graph));
$("#pipeline-list").onfocus = guard(listPipelines);
$("#pipeline-name").onchange = guard(async (e) => {
  const candidate = clone(state.graph); candidate.name = e.target.value || "未命名方案";
  await commit(candidate); await listPipelines();
});
$("#search").oninput = renderCatalog;
$("#favorites").onclick = () => {
  state.onlyFavorites = true; $("#favorites").classList.add("active"); $("#all-components").classList.remove("active"); renderCatalog();
};
$("#all-components").onclick = () => {
  state.onlyFavorites = false; $("#all-components").classList.add("active"); $("#favorites").classList.remove("active"); renderCatalog();
};
$("#run").onclick = guard(() => runPipeline());
$("#remote-reload").onclick = guard(reloadFromServer);
$("#remote-keep").onclick = () => { hideRemoteBanner(); toast("已保留本地修改；下次保存若版本冲突会再次提示"); };
$("#cancel").onclick = guard(async () => {
  await api("cancel_pipeline", {pipeline_id:state.graph.id}); toast("已请求停止，将在当前组件计算结束后停止");
});
$("#validate").onclick = guard(async () => {
  if (state.dirty) await applyParameters();
  await api("validate_pipeline", {pipeline_id:state.graph.id}); toast("校验通过：参数、端口、依赖关系和数据源均有效");
});
$("#copy").onclick = guard(copySelection); $("#delete").onclick = guard(removeSelection);
$("#undo").onclick = guard(() => undo()); $("#redo").onclick = guard(() => undo(true));
$("#layout").onclick = guard(autoLayout); $("#fit").onclick = fitCanvas;
$("#zoom-in").onclick = () => zoom(1.2); $("#zoom-out").onclick = () => zoom(1/1.2);
$("#save").onclick = guard(async () => {
  if (state.dirty) await applyParameters();
  const r = await api("save_pipeline", {pipeline_id:state.graph.id}); toast("已保存：" + r.path);
});
$("#export").onclick = guard(async () => {
  if (state.dirty) await applyParameters();
  const result = await api("get_pipeline_xml", {pipeline_id:state.graph.id});
  const url = URL.createObjectURL(new Blob([result.xml], {type:"application/xml"}));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = state.graph.id + ".xml";
  anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$("#import").onclick = () => $("#xml-file").click();
$("#xml-file").onchange = guard(async (e) => {
  const file = e.target.files[0]; if (!file) return;
  if (file.size > 2000000) throw new Error("XML 大小不能超过 2 MB");
  const result = await api("load_pipeline", {xml:await file.text()});
  await openGraph(result.graph); e.target.value = ""; toast("XML 已导入");
});
$("#upload").onclick = () => $("#csv-file").click();
$("#csv-file").onchange = guard(async (e) => {
  const file = e.target.files[0]; if (!file) return;
  const form = new FormData(); form.append("file", file);
  const response = await fetch("/api/data/upload", {method:"POST",body:form});
  const result = await response.json();
  if (!result.success) throw new Error(result.summary);
  await dataFiles(); await addNode("data.input", undefined, {path:result.path});
  e.target.value = ""; toast("CSV 已上传并添加为数据输入节点");
});
$("#data-files").onchange = guard(async (e) => {
  if (e.target.value) await addNode("data.input", undefined, {path:e.target.value});
  e.target.value = "";
});
$("#checkpoint").onclick = guard(async () => {
  await api("save_checkpoint", {pipeline_id:state.graph.id}); await checkpoints(); toast("检查点已保存到当前进程内存");
});
$("#checkpoint-list").onchange = guard(async (e) => {
  if (!e.target.value) return;
  const r = await api("load_checkpoint", {checkpoint_id:e.target.value}); await openGraph(r.graph); toast("方案和运行数据已恢复");
});
document.querySelectorAll("[data-tab]").forEach((b) => b.onclick = guard(async () => { state.tab = b.dataset.tab; await showResult(); }));
window.addEventListener("keydown", guard(async (e) => {
  if (e.target.matches("input,textarea,select")) return;
  if (e.key === "Escape") { state.pending = null; state.selected.clear(); state.edge = null; renderNodes(); renderInspector(); }
  if (state.running || state.saving) return;
  if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); await removeSelection(); }
  if (e.ctrlKey || e.metaKey) {
    if (e.key.toLowerCase() === "z") { e.preventDefault(); await undo(e.shiftKey); }
    if (e.key.toLowerCase() === "y") { e.preventDefault(); await undo(true); }
    if (e.key.toLowerCase() === "c" && state.selected.size) { e.preventDefault(); await copySelection(); }
    if (e.key.toLowerCase() === "s") { e.preventDefault(); $("#save").click(); }
    if (e.key.toLowerCase() === "a") { e.preventDefault(); state.selected = new Set(state.graph.nodes.map((n) => n.id)); renderNodes(); }
  }
}));
window.addEventListener("resize", fitCanvas);
async function init() {
  subscribeEvents();
  const result = await api("list_components", {limit:100, include_schema:true});
  state.catalog = result.components; renderCatalog();
  const pipelines = await api("list_pipelines");
  if (pipelines.pipelines.length) {
    await openGraph((await api("get_pipeline", {pipeline_id:pipelines.pipelines[0].id})).graph);
  } else await openGraph((await api("create_pipeline", {name:"我的故障预测方案"})).graph);
}
guard(init)();
