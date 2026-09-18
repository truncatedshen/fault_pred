const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clone = (value) => structuredClone(value);
const categories = {
  data: ["数据处理", "▦"], explore: ["数据探索", "◈"], visual: ["数据可视化", "▤"],
  feature: ["特征提取", "ƒ"], validation: ["算法验证", "◇"],
};
// 面板默认宽高与可拖拽范围（像素）。library/inspector 是宽度，results 是高度。
const DEFAULT_LAYOUT = {library: 236, inspector: 278, results: 252};
const PANEL_LIMITS = {library: [170, 460], inspector: [190, 520], results: [120, 560]};
// 方向键微调：向右/上就是"变大"，但右侧面板的"变大"是向左拖，所以分开定义。
const PANEL_KEYS = {
  library: {grow: "ArrowRight", shrink: "ArrowLeft", axis: "x", step: 16, dragSign: 1},
  inspector: {grow: "ArrowLeft", shrink: "ArrowRight", axis: "x", step: 16, dragSign: -1},
  results: {grow: "ArrowUp", shrink: "ArrowDown", axis: "y", step: 24, dragSign: -1},
};
// 组件总数超过该值时，子分类默认折叠：目录变大后先给"目录"，需要时再展开。
const GROUP_AUTO_COLLAPSE = 24;
const state = {
  catalog: [], graph: null, selected: new Set(), edge: null, pending: null,
  zoom: 1, pan: {x: 40, y: 35}, undo: [], redo: [], statuses: {}, running: false,
  saving: false, tab: "result", libraryView: "all", favorites: new Set(), recent: [],
  drag: null, resultRequest: 0, dirty: false, poll: null,
  events: null, syncing: false, remoteVersion: null, nodeMeta: {},
  collapsed: new Set(), expanded: new Set(), groupKeys: [], layout: {...DEFAULT_LAYOUT},
  renderedCollapsed: new Set(),
};
try { state.favorites = new Set(JSON.parse(localStorage.getItem("fault-favorites") || "[]")); } catch {}
try { state.recent = JSON.parse(localStorage.getItem("fault-recent") || "[]").slice(0, 12); } catch {}
try { state.collapsed = new Set(JSON.parse(localStorage.getItem("fault-library-collapsed") || "[]")); } catch {}
try { state.expanded = new Set(JSON.parse(localStorage.getItem("fault-library-expanded") || "[]")); } catch {}
try {
  const saved = JSON.parse(localStorage.getItem("fault-layout") || "{}");
  for (const name of Object.keys(DEFAULT_LAYOUT)) {
    if (Number.isFinite(saved[name])) state.layout[name] = saved[name];
  }
} catch {}
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
  const query = $("#search").value.trim().toLowerCase();
  const categoryFilter = $("#category-filter").value;
  // 搜索、分类筛选或切换收藏/最近视图时强制展开：命中的组件必须看得见。
  const filtering = Boolean(query) || Boolean(categoryFilter) || state.libraryView !== "all";
  const recentOrder = new Map(state.recent.map((type, index) => [type, index]));
  const filtered = state.catalog.filter((c) =>
    (!categoryFilter || c.category === categoryFilter) &&
    (state.libraryView !== "favorites" || state.favorites.has(c.component_type)) &&
    (state.libraryView !== "recent" || recentOrder.has(c.component_type)) &&
    [c.component_type, c.display_name, c.description, c.category, c.subcategory,
      ...(c.tags || []), ...(c.search_keywords || [])].join(" ").toLowerCase().includes(query));
  if (state.libraryView === "recent") filtered.sort((a, b) => recentOrder.get(a.component_type) - recentOrder.get(b.component_type));
  $("#catalog-count").textContent = filtered.length === state.catalog.length ?
    state.catalog.length : filtered.length + "/" + state.catalog.length;
  const componentMarkup = (c, icon) => '<div class="component-item" draggable="true" data-type="' +
    esc(c.component_type) + '" style="--category:var(--' + categoryColor(c.category) +
    ')" title="' + esc(c.description) + '"><span class="component-icon">' + icon +
    "</span><span>" + esc(c.display_name) + "</span><button class=\"star " +
    (state.favorites.has(c.component_type) ? "saved" : "") + '" aria-label="收藏 ' +
    esc(c.display_name) + '">☆</button></div>';
  const categoryOrder = [
    ...Object.keys(categories),
    ...[...new Set(filtered.map((c) => c.category))].filter((category) => !categories[category]),
  ];
  const autoCollapse = state.catalog.length > GROUP_AUTO_COLLAPSE;
  const keys = [];
  const collapsedKeys = [];
  $("#component-library").innerHTML = categoryOrder.map((category) => {
    const components = filtered.filter((c) => c.category === category);
    if (!components.length) return "";
    const [title, icon] = categories[category] || [category, "◇"];
    const categoryKey = groupKey("cat", category);
    keys.push(categoryKey);
    const categoryCollapsed = groupCollapsed("cat", category, false, filtering);
    if (categoryCollapsed) collapsedKeys.push(categoryKey);
    const subcategories = [...new Set(components.map((c) => c.subcategory || "通用"))];
    // 只有一个"通用"子分类的类别（例如可视化）不再套一层，直接列出组件。
    const needsSubcategory = subcategories.length > 1 || subcategories[0] !== "通用";
    const groups = subcategories.map((subcategory) => {
      const members = components.filter((c) => (c.subcategory || "通用") === subcategory);
      if (!needsSubcategory) return members.map((c) => componentMarkup(c, icon)).join("");
      const subcategoryKey = groupKey("sub", category + "/" + subcategory);
      keys.push(subcategoryKey);
      const subcategoryCollapsed = groupCollapsed(
        "sub", category + "/" + subcategory, autoCollapse, filtering);
      if (subcategoryCollapsed) collapsedKeys.push(subcategoryKey);
      const heading = groupHeading("subcategory-title", subcategoryKey, esc(subcategory),
        members.length, subcategoryCollapsed);
      return heading + groupBody(subcategoryCollapsed, members.map((c) => componentMarkup(c, icon)).join(""));
    }).join("");
    return groupHeading("category-title", categoryKey, esc(title), components.length, categoryCollapsed, icon) +
      groupBody(categoryCollapsed, groups);
  }).join("") || '<div class="catalog-empty">没有匹配的组件</div>';
  state.groupKeys = keys;
  state.renderedCollapsed = new Set(collapsedKeys);
  for (const header of document.querySelectorAll(".category-title, .subcategory-title")) {
    // 以"当前渲染出来的状态"取反，而不是看显式记录：默认折叠的分组同样要能一次点开。
    header.onclick = () => toggleGroup(header.dataset.group, header.getAttribute("aria-expanded") === "true");
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
  updateGroupToolbar();
}
function groupKey(kind, id) {
  return kind + ":" + id;
}
// 折叠状态优先级：过滤时强制展开 → 用户显式折叠/展开过 → 目录规模决定的默认值。
function groupCollapsed(kind, id, defaultCollapsed, filtering) {
  if (filtering) return false;
  const key = groupKey(kind, id);
  if (state.collapsed.has(key)) return true;
  if (state.expanded.has(key)) return false;
  return defaultCollapsed;
}
function groupHeading(className, key, label, count, collapsed, icon) {
  return '<button class="' + className + (collapsed ? " collapsed" : "") + '" data-group="' + esc(key) +
    '" aria-expanded="' + String(!collapsed) + '"><i class="chevron">' + (collapsed ? "▸" : "▾") + "</i>" +
    (icon ? '<span class="group-icon">' + icon + "</span>" : "") + label +
    (className === "category-title" ? '<span class="group-count">' : "<span>") + count + "</span></button>";
}
function groupBody(collapsed, markup) {
  return '<div class="group-body' + (collapsed ? " collapsed" : "") + '">' + markup + "</div>";
}
function saveGroupState() {
  localStorage.setItem("fault-library-collapsed", JSON.stringify([...state.collapsed]));
  localStorage.setItem("fault-library-expanded", JSON.stringify([...state.expanded]));
}
function toggleGroup(key, collapsed = null) {
  const next = collapsed === null ? !state.collapsed.has(key) : collapsed;
  if (next) {
    state.collapsed.add(key);
    state.expanded.delete(key);
  } else {
    state.collapsed.delete(key);
    state.expanded.add(key);
  }
  saveGroupState();
  renderCatalog();
}
function allGroupsCollapsed() {
  return state.groupKeys.length > 0 && state.groupKeys.every((key) => state.renderedCollapsed.has(key));
}
function toggleAllGroups() {
  const collapse = !allGroupsCollapsed();
  state.collapsed = new Set(collapse ? state.groupKeys : []);
  state.expanded = new Set(collapse ? [] : state.groupKeys);
  saveGroupState();
  renderCatalog();
  toast(collapse ? "组件库已折叠为目录，点击分组展开" : "组件库已全部展开");
}
function updateGroupToolbar() {
  const button = $("#toggle-groups");
  if (button) button.textContent = allGroupsCollapsed() ? "▸ 展开全部" : "▾ 折叠全部";
}
// ── 面板尺寸 ─────────────────────────────────────────────
// 布局只由 :root 上的三个 CSS 变量描述；状态保存在 localStorage，刷新后保持。
function setPanelSize(name, value) {
  const [min] = PANEL_LIMITS[name];
  state.layout[name] = Math.round(Math.max(min, Math.min(panelCeiling(name), value)));
  applyLayout();
}
// 上限同时受视口限制：侧栏不超过视口宽度的 34%，结果面板不超过高度的 45%，
// 这样无论怎么拖，画布都还留得下可用空间。
function panelCeiling(name) {
  const [, max] = PANEL_LIMITS[name];
  const viewport = name === "results" ? window.innerHeight * 0.45 : window.innerWidth * 0.34;
  return Math.min(max, Math.round(viewport));
}
function persistLayout() {
  localStorage.setItem("fault-layout", JSON.stringify(state.layout));
}
function applyLayout() {
  const root = document.documentElement.style;
  root.setProperty("--library-width", state.layout.library + "px");
  root.setProperty("--inspector-width", state.layout.inspector + "px");
  root.setProperty("--results-height", state.layout.results + "px");
  updateSplitterPositions();
}
// 分隔条按"实际渲染出来的边界"定位：这样宽度钳制与响应式断点都不会让它跑偏。
function updateSplitterPositions() {
  const workspace = $(".workspace");
  const library = $(".library");
  const inspector = $(".inspector");
  const center = $(".center");
  const results = $(".results");
  if (!workspace || !library || !inspector || !center || !results) return;
  if (typeof workspace.getBoundingClientRect !== "function") return;
  const base = workspace.getBoundingClientRect();
  if (!base.width) return;
  const libraryBox = library.getBoundingClientRect();
  const inspectorBox = inspector.getBoundingClientRect();
  const centerBox = center.getBoundingClientRect();
  const resultsBox = results.getBoundingClientRect();
  const librarySplitter = $("#library-splitter");
  if (librarySplitter) librarySplitter.style.left = Math.round(libraryBox.right - base.left - 3) + "px";
  const inspectorSplitter = $("#inspector-splitter");
  if (inspectorSplitter) inspectorSplitter.style.right = Math.round(base.right - inspectorBox.left - 3) + "px";
  const resultsSplitter = $("#results-splitter");
  if (resultsSplitter) {
    resultsSplitter.style.left = Math.round(centerBox.left - base.left) + "px";
    resultsSplitter.style.width = Math.round(centerBox.width) + "px";
    resultsSplitter.style.bottom = Math.round(base.bottom - resultsBox.top - 3) + "px";
  }
}
function nudgePanel(name, direction) {
  const spec = PANEL_KEYS[name];
  if (!spec) return;
  setPanelSize(name, state.layout[name] + (direction === "grow" ? spec.step : -spec.step));
  persistLayout();
}
function startPanelDrag(event, name) {
  if (event.button) return;
  event.preventDefault();
  const handle = event.currentTarget;
  const spec = PANEL_KEYS[name];
  const start = {position: spec.axis === "x" ? event.clientX : event.clientY, size: state.layout[name]};
  handle.classList.add("dragging");
  document.body.classList.add("resizing", spec.axis === "x" ? "col-resize" : "row-resize");
  handle.setPointerCapture?.(event.pointerId);
  const move = (moveEvent) => {
    const current = spec.axis === "x" ? moveEvent.clientX : moveEvent.clientY;
    // 左侧与结果面板"往外拖=变大"，右侧面板方向相反。
    // 方向统一由 PANEL_KEYS.dragSign 描述：底部结果面板与右侧面板的方向相反。
    const delta = spec.dragSign * (current - start.position);
    setPanelSize(name, start.size + delta);
  };
  const finish = () => {
    handle.classList.remove("dragging");
    document.body.classList.remove("resizing", "col-resize", "row-resize");
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", finish);
    handle.removeEventListener("pointercancel", finish);
    persistLayout();
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
}
function resetLayout() {
  state.layout = {...DEFAULT_LAYOUT};
  applyLayout();
  persistLayout();
  toast("面板宽高已恢复默认");
}
function initLayout() {
  applyLayout();
  for (const [selector, name] of [["#library-splitter", "library"],
    ["#inspector-splitter", "inspector"], ["#results-splitter", "results"]]) {
    const handle = $(selector);
    if (!handle) continue;
    // 用 addEventListener 而不是 onpointerdown 属性：属性式监听在 jsdom 里不会触发，
    // 改完之后"拖拽方向"这条逻辑才能被 DOM 测试覆盖到。
    handle.addEventListener("pointerdown", (event) => startPanelDrag(event, name));
    handle.addEventListener("dblclick", resetLayout);
    handle.addEventListener("keydown", (event) => {
      const spec = PANEL_KEYS[name];
      if (event.key === spec.grow || event.key === spec.shrink) {
        event.preventDefault();
        nudgePanel(name, event.key === spec.grow ? "grow" : "shrink");
      }
      if (event.key === "Home") { event.preventDefault(); resetLayout(); }
    });
  }
  window.addEventListener("resize", updateSplitterPositions);
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
// 端口类型标签：输入端口可以声明兼容类型（例如概览同时接受 Dataset 与 FeatureDataset）。
function portTypes(port) {
  return (port.accepted_types && port.accepted_types.length ? port.accepted_types : [port.data_type])
    .join(" | ");
}
function renderNodes() {
  $("#nodes").innerHTML = state.graph.nodes.map((n) => {
    const c = schema(n.type); if (!c) return "";
    const status = nodeStatus(n.id);
    const ports = (kind, specs) => specs.map((p) =>
      '<div class="port ' + kind + (state.pending?.node === n.id && state.pending?.port === p.name ? " active" : "") +
      '" data-node="' + esc(n.id) + '" data-port="' + esc(p.name) + '" data-kind="' + kind +
      '" title="' + esc(portTypes(p) + (p.required ? " · required" : " · optional")) + '">' +
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
  const box = element ? element.getBoundingClientRect() : null;
  // 尺寸量不到时（端口尚未渲染、或环境不支持布局）回退到按节点位置与端口序号推算的坐标，
  // 否则所有端口会塌缩到同一个点，连线全部退化成长度为零的路径。
  if (box && (box.width || box.height)) {
    const canvas = $("#canvas").getBoundingClientRect();
    return {x:(box.left + box.width / 2 - canvas.left - state.pan.x) / state.zoom,
            y:(box.top + box.height / 2 - canvas.top - state.pan.y) / state.zoom};
  }
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  const c = schema(node.type);
  const idx = (kind === "input" ? c.input_ports : c.output_ports).findIndex((p) => p.name === port);
  const offset = kind === "output" ? c.input_ports.length : 0;
  return {x: node.position.x + (kind === "input" ? 12 : 206),
          y: node.position.y + 63 + (offset + idx) * 22};
}
// ── 连线路由 ─────────────────────────────────────────────
// 连线一律走"圆角正交折线"（Simulink 那种直线 + 折角），不用贝塞尔曲线：
// 出端口先水平走一段 stub 再接竖直通道，最后水平进入目标端口。
const EDGE_STUB = 16;    // 出/入端口之后必须保持水平的距离
const EDGE_RADIUS = 7;   // 折角圆角半径（调大就又会变成曲线）
const EDGE_LANE = 9;     // 同一竖直通道上并行连线的错开距离
function round2(value) { return Math.round(value * 100) / 100; }
// 正交折线 → 带圆角的 path。圆角半径按相邻两段中较短的一段收缩，短段也不会被切坏。
function edgePathD(points, radius = EDGE_RADIUS) {
  const commands = ["M" + round2(points[0].x) + "," + round2(points[0].y)];
  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1], cur = points[i], next = points[i + 1];
    const cross = (cur.x - prev.x) * (next.y - cur.y) - (cur.y - prev.y) * (next.x - cur.x);
    if (Math.abs(cross) < 0.01) continue;   // 共线点不是折角，省略掉
    const inLength = Math.hypot(cur.x - prev.x, cur.y - prev.y);
    const outLength = Math.hypot(next.x - cur.x, next.y - cur.y);
    const r = Math.min(radius, inLength / 2, outLength / 2);
    commands.push("L" + round2(cur.x - Math.sign(cur.x - prev.x) * r) + "," +
                  round2(cur.y - Math.sign(cur.y - prev.y) * r));
    commands.push("Q" + round2(cur.x) + "," + round2(cur.y) + " " +
                  round2(cur.x + Math.sign(next.x - cur.x) * r) + "," +
                  round2(cur.y + Math.sign(next.y - cur.y) * r));
  }
  const last = points[points.length - 1];
  commands.push("L" + round2(last.x) + "," + round2(last.y));
  return commands.join(" ");
}
// 正交路由：首尾严格落在两个端口圆心，中间只有水平段与竖直段。
// 常规情况（目标在右侧）是一条"Z"：右 stub → 竖直通道 → 水平进端口；
// 回边或两节点几乎重叠时改走"U"：绕到两行之间，避免横穿节点。
function orthogonalRoute(a, b, lane = 0) {
  if (b.x - a.x >= EDGE_STUB * 2) {
    const stub = EDGE_STUB;
    const channel = (a.x + b.x) / 2 + lane;   // 两端口之间的空档中线
    return [a, {x: a.x + stub, y: a.y}, {x: channel, y: a.y},
            {x: channel, y: b.y}, {x: b.x - stub, y: b.y}, b];
  }
  const stub = EDGE_STUB * 2;   // 回边要绕开节点本体，出线留得更宽
  const sameRow = Math.abs(b.y - a.y) < EDGE_LANE * 2;
  const alley = (a.y + b.y) / 2 + lane + (sameRow ? 54 : 0);
  return [a, {x: a.x + stub, y: a.y}, {x: a.x + stub, y: alley},
          {x: b.x - EDGE_STUB, y: alley}, {x: b.x - EDGE_STUB, y: b.y}, b];
}
// 同一条竖直通道上并行的连线互相错开，避免叠成一条看不清的粗线。
function claimLane(occupied, a, b) {
  const top = Math.min(a.y, b.y), bottom = Math.max(a.y, b.y), base = (a.x + b.x) / 2;
  for (let step = 0; step < 12; step++) {
    const lane = (step % 2 ? -1 : 1) * Math.ceil(step / 2) * EDGE_LANE;
    const clash = occupied.some((item) => Math.abs(item.x - (base + lane)) < EDGE_LANE - 1 &&
      Math.min(item.bottom, bottom) - Math.max(item.top, top) > -1);
    if (!clash) { occupied.push({x: base + lane, top, bottom}); return lane; }
  }
  return 0;
}
function drawEdges() {
  if (!state.graph) return;
  const occupied = [];
  $("#connections").innerHTML = state.graph.edges.map((e, index) => {
    const a = portPosition(e.source_node, e.source_port, "output");
    const b = portPosition(e.target_node, e.target_port, "input");
    const d = edgePathD(orthogonalRoute(a, b, claimLane(occupied, a, b)));
    const title = esc(e.source_node + "." + e.source_port + " → " + e.target_node + "." + e.target_port);
    return '<path data-edge="' + index + '" class="' + (state.edge === index ? "selected" : "") +
      '" d="' + d + '"><title>' + title + "</title></path>" +
      // 1.8px 的折线太难点中，再叠一条透明粗线当点击热区（热区永远保持透明）。
      '<path data-edge-hit="' + index + '" class="edge-hit" d="' + d + '"><title>' + title +
      "</title></path>";
  }).join("");
  if (state.pending?.point) {
    const a = portPosition(state.pending.node, state.pending.port, "output");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", "pending");
    path.setAttribute("d", edgePathD(orthogonalRoute(a, state.pending.point, 0)));
    $("#connections").append(path);
  }
  $("#connections").querySelectorAll("[data-edge],[data-edge-hit]").forEach((path) => {
    const index = Number(path.dataset.edge ?? path.dataset.edgeHit);
    path.onclick = (e) => {
      e.stopPropagation(); state.edge = index; state.selected.clear();
      renderNodes(); renderInspector(); showResult();
    };
    if (path.dataset.edgeHit === undefined) return;
    // 悬停在热区上时高亮真正那条线，热区自己保持透明。
    const visible = () => $("#connections").querySelector('[data-edge="' + index + '"]');
    path.onmouseenter = () => visible()?.classList.add("hover");
    path.onmouseleave = () => visible()?.classList.remove("hover");
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
  state.recent = [type, ...state.recent.filter((item) => item !== type)].slice(0, 12);
  localStorage.setItem("fault-recent", JSON.stringify(state.recent));
  renderCatalog();
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
    [...c.input_ports.map((p) => "↳ " + p.name + " : " + portTypes(p)),
     ...c.output_ports.map((p) => "↗ " + p.name + " : " + portTypes(p))].map((s) =>
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
  const keys = [["accuracy","Accuracy"],["balanced_accuracy","Balanced acc."],["precision","Precision"],
    ["recall","Recall"],["f1","F1 score"],["roc_auc","ROC-AUC"],["average_precision","PR-AUC"],["miss_rate","漏报率"]];
  return '<div class="metric-grid">' + keys.map(([k, title]) => '<div class="metric-card"><span class="label">' +
    title + "</span><strong>" + (typeof m[k] === "number" ? (m[k] * 100).toFixed(1) + "%" : "—") +
    "</strong></div>").join("") + '</div><p class="metric-note">' + esc(m.algorithm) + " · " + esc(m.split_method) +
    " · 训练 " + m.train_count + " / 测试 " + m.test_count + "</p>" +
    classBalance(m) +
    (m.warnings || []).map((w) => '<div class="warning">' + esc(w) + "</div>").join("") +
    (m.confusion_matrix ? table(m.confusion_matrix.map((row, i) =>
      Object.fromEntries([["实际 / 预测", m.classes[i]], ...row.map((v, j) => [String(m.classes[j]), v])]))): "");
}
function percent(value) { return typeof value === "number" ? (value * 100).toFixed(2) + "%" : "—"; }
/* 训练集/测试集的正负样本构成：只有数量看不出"228 行测试集里几行是故障"，占比才是那个数字。 */
function classBalance(m) {
  const sides = [["训练集", m.train_class_counts, m.train_class_rates, m.train_count],
                 ["测试集", m.test_class_counts, m.test_class_rates, m.test_count]];
  if (!sides.some(([, counts]) => counts && Object.keys(counts).length)) return "";
  const classes = (m.classes && m.classes.length ? m.classes :
    Object.keys(m.test_class_counts || m.train_class_counts || {})).map(String);
  const rows = [];
  for (const [side, counts, rates, total] of sides) {
    if (!counts) continue;
    const sum = total || Object.values(counts).reduce((a, b) => a + b, 0);
    for (const cls of classes) {
      const count = counts[cls] ?? 0;
      const rate = rates && typeof rates[cls] === "number" ? rates[cls] : (sum ? count / sum : 0);
      rows.push({集合: side, 类别: cls, 数量: count, 占比: percent(rate),
        角色: cls === m.positive_class ? "正类" : (classes.length === 2 ? "负类" : "—")});
    }
  }
  return '<h4 class="metric-title">正负样本构成</h4>' + table(rows) +
    (m.positive_class ? '<p class="metric-note">正类 = ' + esc(String(m.positive_class)) + '</p>' : "");
}
/* 数据概览里的标签构成（label_column 或 labels 端口给了才会有）。 */
function labelDistributionView(d) {
  if (!d || !d.total) return "";
  const rows = (d.classes || []).map((cls) => ({类别: cls, 数量: d.counts[cls], 占比: percent(d.rates[cls]),
    角色: cls === d.positive_class ? "正类" : (d.binary ? "负类" : "—")}));
  const headline = d.positive_class != null && typeof d.positive_rate === "number" ?
    " · 正类 " + esc(String(d.positive_class)) + " " + d.positive_count + "/" + d.total +
    "（" + percent(d.positive_rate) + "）" : "";
  return '<h4 class="metric-title">标签构成（' + esc(String(d.source)) + "）" + headline + "</h4>" +
    table(rows) + (d.findings || []).map((f) => '<div class="warning">' + esc(f) + "</div>").join("");
}
/* 导出该节点的中间产物：输入侧=上游喂给它的数据，输出侧=它自己产出的表。
   走的是 /api/node-data 这对文件级接口，浏览器直接下载，不用把大表塞进 JSON 响应。 */
function exportUrl(row, nodeId, extra = {}) {
  const query = new URLSearchParams({
    pipeline_id: state.graph.id, node_id: nodeId,
    direction: row.direction, port: row.port, ...extra,
  });
  return "/api/node-data/csv?" + query.toString();
}
function exportView(spec, nodeId) {
  const usable = (spec.data || []).filter((row) => row.available);
  if (!usable.length) {
    const blocked = (spec.data || []).filter((row) => !row.available);
    if (!blocked.length) return "";
    return '<section class="result-port export-block"><h3>导出数据（CSV）</h3>' +
      blocked.map((row) => '<div class="export-row"><div>' + exportLabel(row) +
        '</div><span class="metric-note">' + esc(row.reason || "不可导出") + "</span></div>").join("") +
      "</section>";
  }
  const rows = spec.data.map((row) => {
    const label = exportLabel(row);
    if (!row.available) {
      return '<div class="export-row">' + label +
        '<span class="metric-note">' + esc(row.reason || "不可导出") + "</span></div>";
    }
    const size = row.rows.toLocaleString() + " 行 × " + row.columns.length + " 列";
    const cap = spec.default_max_rows || 0;
    const big = cap > 0 && row.rows > cap;
    const buttons = ['<a class="export-button" download href="' +
      esc(exportUrl(row, nodeId, big ? {max_rows: cap} : {})) + '">' +
      (big ? "下载前 " + cap.toLocaleString() + " 行" : "下载 CSV") + "</a>"];
    if (big) buttons.push('<a class="export-button muted" download href="' +
      esc(exportUrl(row, nodeId, {max_rows: 0})) + '">全部 ' + row.rows.toLocaleString() + " 行</a>");
    return '<div class="export-row"><div>' + label +
      ' <span class="metric-note">' + size + "</span></div><div>" + buttons.join("") + "</div></div>";
  }).join("");
  return '<section class="result-port export-block"><h3>导出数据（CSV）</h3>' +
    '<p class="metric-note">在本机用 Excel / pandas 核对这一层的中间产物：' +
    "输入侧 = 上游喂给它的数据，输出侧 = 它自己产出的表。UTF-8 with BOM，特征表保留窗口键索引。</p>" +
    rows + "</section>";
}
function exportLabel(row) {
  const side = row.direction === "input" ? "输入" : "输出";
  const owner = row.direction === "input" && row.owner !== state.exportNode ?
    '<span class="metric-note"> 来自 ' + esc(row.owner) + " · " + esc(row.owner_port) + "</span>" : "";
  return '<span class="export-tag ' + row.direction + '">' + side + "</span>" +
    "<code>" + esc(row.port) + "</code>" + owner;
}
async function nodeDataView(nodeId) {
  try {
    const query = new URLSearchParams({pipeline_id: state.graph.id, node_id: nodeId});
    const response = await fetch("/api/node-data?" + query.toString());
    const spec = await response.json();
    if (!response.ok || !spec.data) return "";
    return exportView(spec, nodeId);
  } catch (e) { return ""; }
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
      labelDistributionView(value.label_distribution) +
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
      // 导出入口单独取一次：拿不到就静默省略，绝不让它把已经渲染好的结果顶掉。
      html += await nodeDataView(node.id);
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
function setLibraryView(view) {
  state.libraryView = view;
  const active = view === "all" ? "all-components" : view === "favorites" ? "favorites" : "recent-components";
  for (const id of ["all-components", "favorites", "recent-components"]) {
    $("#" + id).classList.toggle("active", id === active);
  }
  renderCatalog();
}
$("#favorites").onclick = () => setLibraryView("favorites");
$("#all-components").onclick = () => setLibraryView("all");
$("#recent-components").onclick = () => setLibraryView("recent");
$("#category-filter").onchange = renderCatalog;
$("#toggle-groups").onclick = toggleAllGroups;
$("#reset-layout").onclick = resetLayout;
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
  initLayout();
  const result = await api("list_components", {limit:500, include_schema:true});
  state.catalog = result.components;
  $("#category-filter").innerHTML = '<option value="">全部分类</option>' +
    [...new Set(state.catalog.map((c) => c.category))].map((category) =>
      '<option value="' + esc(category) + '">' + esc(categories[category]?.[0] || category) + "</option>").join("");
  renderCatalog();
  const pipelines = await api("list_pipelines");
  if (pipelines.pipelines.length) {
    await openGraph((await api("get_pipeline", {pipeline_id:pipelines.pipelines[0].id})).graph);
  } else await openGraph((await api("create_pipeline", {name:"我的故障预测方案"})).graph);
}
guard(init)();
