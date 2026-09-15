"""Run a graph through Python without HTTP, UI, MCP or an LLM."""

from pathlib import Path

from fault_platform.examples import create_dataset, example_graph
from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.workspace import FaultWorkspace
from fault_platform.xml_io import XMLSerializer


def main() -> None:
    root = Path(__file__).resolve().parent
    create_dataset(root / "data" / "synthetic_equipment.csv")
    graph = example_graph(default_registry())
    workspace = FaultWorkspace(graph.pipeline_id)
    ExecutionEngine().execute(graph, ExecutionContext(workspace, root / "data"))
    XMLSerializer().save(graph, root / "python_api_pipeline.xml")
    print("Pipeline status:", workspace.status)
    print("Random Forest accuracy:", workspace.get_output("forest", "metrics")["accuracy"])
    print("Window features:", workspace.get_output("stat", "features").shape)


if __name__ == "__main__":
    main()
