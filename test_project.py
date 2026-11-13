from analyzer import (
    build_architecture,
    build_folder_view,
    graph_stats,
    normalize_graph,
)
from knowledge import chunk_source, language_for_path


RAW = {
    "nodes": [
        {
            "id": "page",
            "label": "HomePage",
            "source_file": "src/pages/home.tsx",
        },
        {
            "id": "nav",
            "label": "Navbar",
            "source_file": "src/components/navigation/Navbar.tsx",
        },
        {
            "id": "button",
            "label": "Button",
            "source_file": "src/components/ui/Button.tsx",
        },
        {
            "id": "service",
            "label": "UserService",
            "source_file": "src/services/user.ts",
        },
        {
            "id": "external",
            "label": "Any",
            "source_file": "",
        },
    ],
    "edges": [
        {
            "source": "page",
            "target": "nav",
            "relation": "imports",
        },
        {
            "source": "nav",
            "target": "button",
            "relation": "imports",
        },
        {
            "source": "page",
            "target": "service",
            "relation": "calls",
        },
        {
            "source": "external",
            "target": "service",
            "relation": "uses",
        },
    ],
}


def test_normalization():
    graph = normalize_graph(RAW)
    assert graph_stats(graph)["nodes"] == 5


def test_architecture_ignores_fileless_symbols():
    architecture = build_architecture(
        normalize_graph(RAW),
        "demo",
    )

    labels = {
        component["raw_label"]
        for component in architecture["components"]
    }

    assert "Any" not in labels


def test_folder_view_keeps_nested_folders():
    graph = normalize_graph(RAW)

    view = build_folder_view(
        graph,
        "src/components",
    )

    folders = {
        component["raw_label"]
        for component in view["components"]
        if component["node_kind"] == "folder"
    }

    assert "navigation" in folders
    assert "ui" in folders


def test_nested_folder_reveals_real_file():
    graph = normalize_graph(RAW)

    view = build_folder_view(
        graph,
        "src/components/navigation",
    )

    files = {
        component["file_path"]
        for component in view["components"]
        if component["node_kind"] == "file"
    }

    assert "src/components/navigation/Navbar.tsx" in files


def test_code_chunker():
    source = "\n".join(
        [
            "export function alpha() {",
            "  return 1;",
            "}",
        ]
        * 40
    )

    chunks = chunk_source(
        "src/example.ts",
        source,
    )

    assert chunks
    assert chunks[0]["language"] == "typescript"
    assert chunks[0]["start_line"] == 1


def test_language_detection():
    assert language_for_path("thing.tsx") == "tsx"
    assert language_for_path("thing.py") == "python"


def test_model_classification_separates_chat_and_embeddings():
    from knowledge import OMLXClient

    groups = OMLXClient.classify_models([
        "bge-m3-mlx-fp16",
        "Qwen3.8-27B-OBLITERATED-OptiQ-4bit",
        "bge-reranker-v2",
    ])

    assert groups["embedding"] == ["bge-m3-mlx-fp16"]
    assert groups["chat"] == ["Qwen3.8-27B-OBLITERATED-OptiQ-4bit"]
    assert groups["reranker"] == ["bge-reranker-v2"]


def test_compact_result_focuses_matching_lines():
    from knowledge import compact_result

    content = "\n".join(
        ["noise %d" % i for i in range(40)]
        + ["function BusinessModelCanvas() {"]
        + ["important state update"]
        + ["noise tail %d" % i for i in range(40)]
    )

    result = compact_result(
        {
            "path": "src/BusinessModelCanvas.tsx",
            "content": content,
            "start_line": 101,
            "end_line": 182,
            "symbol": "BusinessModelCanvas",
        },
        "How does BusinessModelCanvas update state?",
        max_lines=20,
    )

    assert "BusinessModelCanvas" in result["content"]
    assert len(result["content"].splitlines()) <= 20
    assert result["start_line"] > 101


def test_budget_results_limits_context_and_sources():
    from knowledge import budget_results

    rows = [
        {
            "path": "src/file%d.py" % i,
            "content": "target\n" + ("x" * 1200),
            "start_line": 1,
            "end_line": 30,
            "symbol": "target",
        }
        for i in range(10)
    ]

    compact = budget_results(
        rows,
        "target",
        char_budget=2600,
        limit=4,
    )

    assert 1 <= len(compact) <= 4
    assert sum(len(item["content"]) for item in compact) < 3200


def test_symbol_aware_chunker_keeps_function_name():
    from knowledge import chunk_source

    source = "\n".join([
        "import os",
        "",
        "def alpha():",
        "    return 1",
        "",
        "def beta():",
        "    return 2",
    ])

    chunks = chunk_source("src/example.py", source)
    symbols = [item["symbol"] for item in chunks if item.get("symbol")]
    assert "alpha" in symbols
    assert "beta" in symbols


def test_spec_task_normalization():
    from spec_engine import normalize_tasks

    tasks = normalize_tasks({
        "tasks": [
            {"title": "Create storage", "detail": "Add local persistence"},
            "Build the UI",
        ]
    })

    assert tasks[0]["id"] == "T001"
    assert tasks[1]["id"] == "T002"
    assert tasks[0]["status"] == "pending"


def test_extract_json_handles_fenced_and_surrounded_json():
    from spec_engine import extract_json

    assert extract_json("prefix\n```json\n{\"tasks\": []}\n```\nsuffix") == {"tasks": []}


def test_validate_file_payload_rejects_unsafe_paths():
    from forge import validate_file_payload
    import pytest

    for path in ("/absolute.py", "../escape.py", "C:/outside.py", "~/outside.py"):
        with pytest.raises(RuntimeError):
            validate_file_payload({"files": [{"path": path, "content": "x"}]})


def test_validate_file_payload_accepts_valid_files():
    from forge import validate_file_payload

    assert validate_file_payload({
        "files": [{"path": "src/main.py", "content": "print('ok')"}],
    }) == [{"path": "src/main.py", "content": "print('ok')"}]


def test_exact_code_queries_can_skip_embedding():
    from agent import _looks_like_exact_code_query

    assert _looks_like_exact_code_query("Explain `createSession`")
    assert _looks_like_exact_code_query("Open src/auth/session.ts")
    assert _looks_like_exact_code_query("What does BusinessModelCanvas do?")
    assert not _looks_like_exact_code_query("Explain the overall architecture")


def test_spec_engine_mock_pipeline():
    from spec_engine import SpecEngine

    class MockOMLX:
        def __init__(self):
            self.responses = [
                "## Goal\nBuild a calculator.",
                "## Stack\nPython and Tkinter.",
                '{"tasks":[{"title":"Create UI","detail":"Create the calculator window"}]}',
            ]

        def chat(self, messages, model, **kwargs):
            return self.responses.pop(0)

    engine = SpecEngine(MockOMLX())
    spec = engine.generate_spec("calculator", "mock", guidance="official specify guidance")
    plan = engine.generate_plan(spec, "mock", guidance="official plan guidance")
    tasks = engine.generate_tasks(spec, plan, "mock", guidance="official tasks guidance")

    assert "calculator" in spec.lower()
    assert "tkinter" in plan.lower()
    assert tasks[0]["id"] == "T001"
    assert tasks[0]["title"] == "Create UI"
