import pytest

from fault_platform.components.base import DataType
from fault_platform.graph import ComponentGraph
from fault_platform.runtime import ExecutionEngine
from fault_platform.workspace import NodeStatus, PipelineStatus, WorkspaceManager
from fault_platform.xml_io import XMLParser, XMLSerializer


def test_connections_cycles_and_order(pipeline):
    assert not pipeline.validate_graph()
    order = pipeline.topological_sort()
    assert order.index("source") < order.index("features") < order.index("model")
    with pytest.raises(ValueError, match="port"):
        pipeline.connect("filter", "dataset", "source", "dataset")  # no input on source


def test_illegal_ports_and_actual_cycle(registry):
    g = ComponentGraph(registry)
    for n in ["a", "b"]:
        g.add_node("data.standardization", n)
    g.connect("a", "dataset", "b", "dataset")
    with pytest.raises(ValueError, match="cycle"):
        g.connect("b", "dataset", "a", "dataset")
    g.add_node("visual.overview", "v")
    with pytest.raises(ValueError, match="Incompatible"):
        g.connect("v", "overview", "a", "dataset")
    with pytest.raises(ValueError):
        g.connect("a", "dataset", "b", "dataset")
    assert len(g.edges) == 1
    assert g.validate_graph()


def test_xml_roundtrip_and_reject_bad_xml(pipeline, registry):
    pipeline.metadata = {"description": "中文 & <xml>", "seed": 42}
    pipeline.ui = {"annotations": [{"text": "测试", "x": 3}]}
    xml = XMLSerializer().dumps(pipeline)
    restored = XMLParser(registry).loads(xml, require_complete=True)
    assert restored.serialize() == pipeline.serialize()
    for bad in [
        xml.replace('dataType="Dataset"', 'dataType="Model"', 1),
        xml.replace('version="1.0" graphVersion', 'version="99.0" graphVersion'),
        '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]><x>&e;</x>',
    ]:
        with pytest.raises(ValueError):
            XMLParser(registry).loads(bad)


def test_xml_tolerates_a_new_optional_port_but_not_a_changed_one(pipeline, registry):
    """端口"只增一个可选口"是向后兼容的：旧 XML 不该因为平台加了个可选输入就废掉。

    实测背景：给 `visual.overview` 加可选 `labels` 输入端口之后，之前保存的 HBM 方案
    报 `Port declarations differ from registry: overview`，等于一次向后兼容的改动把所有旧
    方案作废。所以这里放行"旧文档不认识的新可选端口"，其余不一致照旧拒绝。
    """
    import xml.etree.ElementTree as ElementTree

    def overview_ports(root: ElementTree.Element) -> list[ElementTree.Element]:
        node = next(item for item in root.findall("./nodes/node") if item.get("id") == "overview")
        return list(node.find("ports"))

    def mutate(document: str, edit) -> str:
        root = ElementTree.fromstring(document)
        edit(root)
        return ElementTree.tostring(root, encoding="unicode")

    document = XMLSerializer().dumps(pipeline)
    assert [
        item.get("name")
        for item in overview_ports(ElementTree.fromstring(document))
        if item.get("required") == "false"
    ] == ["labels"]

    def drop_optional(root):
        node = next(item for item in root.findall("./nodes/node") if item.get("id") == "overview")
        for item in [p for p in node.find("ports") if p.get("required") == "false"]:
            node.find("ports").remove(item)

    older = mutate(document, drop_optional)
    restored = XMLParser(registry).loads(older)
    assert "overview" in restored.nodes
    assert restored.validate_graph() == []

    # 改名 = 未知端口，必须拒绝（否则手改 XML 就能绕过类型系统）。
    def rename_optional(root):
        node = next(item for item in root.findall("./nodes/node") if item.get("id") == "overview")
        for item in node.find("ports"):
            if item.get("name") == "labels":
                item.set("name", "not_a_port")

    with pytest.raises(ValueError, match="Port declarations differ"):
        XMLParser(registry).loads(mutate(document, rename_optional))

    # 少一个必填端口也必须拒绝。
    def drop_required(root):
        node = next(item for item in root.findall("./nodes/node") if item.get("id") == "overview")
        for item in [p for p in node.find("ports") if p.get("required") == "true"]:
            node.find("ports").remove(item)
            return

    with pytest.raises(ValueError, match="Port declarations differ"):
        XMLParser(registry).loads(mutate(document, drop_required))


def test_execution_and_incremental_invalidation(pipeline, context):
    engine = ExecutionEngine()
    ws = engine.execute(pipeline, context)
    assert ws.status == PipelineStatus.SUCCESS
    assert ws.get_output("model", "metrics")["accuracy"] > 0.8
    count = len(ws.history)
    engine.execute(pipeline, context)
    assert all(h.cached for h in ws.history[count:])
    pipeline.configure("filter", {"value": 0.1})
    count = len(ws.history)
    engine.execute(pipeline, context)
    cached = {h.node_id for h in ws.history[count:] if h.cached}
    assert cached == {"source", "overview"}
    # Mutating a retrieved object cannot change a branch's cached artifact.
    frame = ws.get_output("source", "dataset")
    frame.iloc[0, 0] = -100
    assert ws.get_output("source", "dataset").iloc[0, 0] != -100


def test_failure_propagation_retry(pipeline, context):
    pipeline.configure("filter", {"column": "does_not_exist"})
    ws = ExecutionEngine().execute(pipeline, context)
    assert ws.node_status["filter"] == NodeStatus.FAILED
    assert ws.node_status["features"] == NodeStatus.SKIPPED
    assert ws.node_status["overview"] == NodeStatus.SUCCESS
    assert "stack" in ws.errors["filter"]
    pipeline.configure("filter", {"column": "vibration"})
    ExecutionEngine().execute(pipeline, context, mode="from", node_id="filter")
    assert ws.status == PipelineStatus.SUCCESS


def test_checkpoint_is_independent(pipeline, context):
    ws = ExecutionEngine().execute(pipeline, context)
    manager = WorkspaceManager()
    manager.workspaces[ws.workspace_id] = ws
    cp = manager.save_checkpoint(ws.workspace_id, pipeline.serialize())
    ws.clear_node("model")
    restored, graph = manager.load_checkpoint(cp.checkpoint_id)
    assert restored.node_status["model"] == NodeStatus.SUCCESS
    assert graph == pipeline.serialize()
    restored.clear_node("source")
    restored, _ = manager.load_checkpoint(cp.checkpoint_id)
    assert restored.get_output("source", "dataset").shape[0] == 640


def test_feature_branches_are_inspectable(pipeline, context, registry):
    """中间产物必须能被检验：检查类组件可以挂在特征分支上，转换类组件不会因此变宽松。"""
    port = next(p for p in registry.get("visual.overview").input_ports if p.name == "dataset")
    assert port.data_type is DataType.DATASET
    assert port.accepted_types == (DataType.DATASET, DataType.FEATURE_DATASET)
    # 只有检查类组件放宽：数据转换仍然只吃原始 Dataset。
    assert registry.get("data.filter").input_ports[0].accepted_types == (DataType.DATASET,)

    pipeline.add_node("visual.overview", "feature_overview")
    pipeline.connect("features", "features", "feature_overview", "dataset")
    with pytest.raises(ValueError, match="Incompatible port types"):
        # 标签向量不是"表"，接进来仍然要被拒绝。
        pipeline.connect("features", "labels", "feature_overview", "dataset")
    assert not pipeline.validate_graph()

    ws = ExecutionEngine().execute(pipeline, context)
    assert ws.status == PipelineStatus.SUCCESS
    features = ws.get_output("features", "features")
    payload = ws.get_output("feature_overview", "overview")
    assert payload["row_count"] == features.shape[0]
    assert payload["column_names"] == list(features.columns)
