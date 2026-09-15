import pytest

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
