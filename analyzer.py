from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
import hashlib
import posixpath
import re

MAX_NODES = 30000
MAX_EDGES = 120000
MAX_TEXT = 240

ALLOWED_RELATIONS = {
    "calls", "imports", "imports_from", "inherits", "implements",
    "references", "contains", "uses", "depends_on", "mixes_in",
    "shares_data_with", "conceptually_related_to",
    "semantically_similar_to",
}

IGNORED_PATH_PARTS = {
    ".git", "node_modules", "dist", "build", "coverage", "__pycache__",
    ".venv", "venv", "vendor", ".next", ".cache", ".turbo", "target",
}

CONFIG_NAMES = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "requirements.txt", "dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "vite.config.ts",
    "vite.config.js", "eslint.config.js", "eslint.config.mjs",
    "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json",
}


class AnalysisError(RuntimeError):
    pass


def _text(value, limit=MAX_TEXT):
    text = str(value or "").replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text[:limit]


def _safe_path(value):
    value = _text(value, 420).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return str(path)[:420]


def normalize_graph(raw):
    if not isinstance(raw, dict):
        raise AnalysisError("Graph output is not a JSON object.")

    raw_nodes = raw.get("nodes", [])
    raw_edges = raw.get("links", raw.get("edges", []))

    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise AnalysisError("Graph nodes/edges are malformed.")

    if len(raw_nodes) > MAX_NODES or len(raw_edges) > MAX_EDGES:
        raise AnalysisError("Repository graph exceeds SoftwareForge safety limits.")

    nodes = []
    ids = set()

    for item in raw_nodes:
        if not isinstance(item, dict):
            continue

        node_id = _text(item.get("id"), 180)
        if not node_id or node_id in ids:
            continue

        ids.add(node_id)
        nodes.append({
            "id": node_id,
            "label": _text(item.get("label") or node_id, 120),
            "kind": _text(
                item.get("kind")
                or item.get("node_type")
                or item.get("type")
                or item.get("file_type")
                or "code",
                40,
            ),
            "file": _safe_path(
                item.get("source_file")
                or item.get("file")
                or ""
            ),
            "location": _text(item.get("source_location") or "", 80),
        })

    edges = []

    for item in raw_edges:
        if not isinstance(item, dict):
            continue

        source = _text(item.get("source"), 180)
        target = _text(item.get("target"), 180)

        if source not in ids or target not in ids or source == target:
            continue

        relation = _text(
            item.get("relation")
            or item.get("type")
            or item.get("label")
            or "uses",
            50,
        )

        if relation not in ALLOWED_RELATIONS:
            relation = "uses"

        edges.append({
            "source": source,
            "target": target,
            "relation": relation,
        })

    return {"nodes": nodes, "edges": edges}


def graph_stats(graph):
    files = {
        node["file"]
        for node in graph.get("nodes", [])
        if node.get("file")
    }

    return {
        "nodes": len(graph.get("nodes", [])),
        "edges": len(graph.get("edges", [])),
        "files": len(files),
    }


def _is_repo_file(path_text):
    if not path_text:
        return False

    parts = PurePosixPath(path_text).parts

    if not parts:
        return False

    return not any(
        part.lower() in IGNORED_PATH_PARTS
        for part in parts
    )


def repository_files(graph):
    return sorted({
        node["file"]
        for node in graph.get("nodes", [])
        if _is_repo_file(node.get("file", ""))
    })


def _bucket_for_path(path_text):
    path = PurePosixPath(path_text)
    parts = list(path.parts)

    if not parts:
        return None

    lowered = [part.lower() for part in parts]
    filename = lowered[-1]

    if (
        len(parts) == 1
        and (
            filename in CONFIG_NAMES
            or filename.startswith("tsconfig")
            or ".config." in filename
        )
    ):
        return ("Tooling & Config", True)

    if any(part in {"test", "tests", "__tests__"} for part in lowered):
        return ("Tests", True)

    if any(part in {"doc", "docs", "documentation"} for part in lowered):
        return ("Docs", True)

    if any(part in {"script", "scripts", "tools"} for part in lowered):
        return ("Scripts", True)

    while lowered and lowered[0] in {
        "src", "app", "source", "lib", "server", "backend", "frontend",
    }:
        lowered = lowered[1:]
        parts = parts[1:]

    if not parts or len(parts) == 1:
        return ("Core", False)

    bucket = parts[0]

    if bucket.lower() == "packages" and len(parts) > 1:
        bucket = parts[1]

    return (bucket, False)


def _component_type(name):
    lowered = name.lower()

    if any(x in lowered for x in ("auth", "security", "identity", "session", "oauth", "jwt")):
        return "security"

    if any(x in lowered for x in (
        "component", "components", "page", "pages", "screen", "view",
        "ui", "client", "frontend", "web",
    )):
        return "frontend"

    if any(x in lowered for x in (
        "data", "db", "database", "model", "models", "store",
        "storage", "schema", "repository",
    )):
        return "database"

    if any(x in lowered for x in ("worker", "workers", "task", "queue", "event", "job")):
        return "worker"

    if any(x in lowered for x in ("tooling", "config", "infra", "deploy", "docker", "scripts")):
        return "infra"

    if any(x in lowered for x in ("external", "integration", "provider", "sdk")):
        return "external"

    return "service"


def _clean_label(name, limit=24):
    value = str(name).replace("_", " ").replace("-", " ").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _common_folder(files):
    if not files:
        return ""

    try:
        common = posixpath.commonpath(files)
    except ValueError:
        return ""

    if "." in PurePosixPath(common).name:
        common = str(PurePosixPath(common).parent)

    return "" if common == "." else common


def _aggregate_connections(graph, node_to_bucket, valid_buckets, id_map):
    pair_data = {}

    for edge in graph.get("edges", []):
        source_bucket = node_to_bucket.get(edge["source"])
        target_bucket = node_to_bucket.get(edge["target"])

        if (
            not source_bucket
            or not target_bucket
            or source_bucket == target_bucket
            or source_bucket not in valid_buckets
            or target_bucket not in valid_buckets
        ):
            continue

        unordered = tuple(sorted((source_bucket, target_bucket)))
        data = pair_data.setdefault(
            unordered,
            {
                "directions": defaultdict(int),
                "relations": defaultdict(int),
            },
        )

        data["directions"][(source_bucket, target_bucket)] += 1
        data["relations"][edge["relation"]] += 1

    rows = []

    for data in pair_data.values():
        strongest_direction = max(
            data["directions"],
            key=data["directions"].get,
        )

        relation = max(
            data["relations"],
            key=data["relations"].get,
        )

        total = sum(data["directions"].values())

        rows.append((
            total,
            strongest_direction,
            relation,
            len(data["directions"]) > 1,
        ))

    rows.sort(key=lambda row: row[0], reverse=True)

    connections = []

    for index, (total, direction, relation, bidirectional) in enumerate(rows[:48]):
        source, target = direction

        connections.append({
            "id": "edge_%d" % (index + 1),
            "from": id_map[source],
            "to": id_map[target],
            "relation": relation,
            "count": total,
            "bidirectional": bidirectional,
        })

    return connections


def build_architecture(graph, title="Software Architecture", max_components=16):
    groups = defaultdict(list)
    group_files = defaultdict(set)
    group_auxiliary = {}
    node_group = {}

    for node in graph.get("nodes", []):
        file_name = node.get("file", "")

        if not _is_repo_file(file_name):
            continue

        bucket = _bucket_for_path(file_name)

        if not bucket:
            continue

        group_name, auxiliary = bucket

        groups[group_name].append(node["id"])
        group_files[group_name].add(file_name)
        group_auxiliary[group_name] = auxiliary
        node_group[node["id"]] = group_name

    ranked = sorted(
        groups,
        key=lambda group: (
            bool(group_auxiliary.get(group, False)),
            -len(groups[group]),
        ),
    )[:max_components]

    ranked_set = set(ranked)
    ids = {}
    used = set()

    for index, group in enumerate(ranked):
        base = re.sub(r"[^A-Za-z0-9_-]", "_", group)

        if not base or not base[0].isalpha():
            base = "module_%d" % (index + 1)

        component_id = base[:32]

        while component_id in used:
            component_id = "%s_%d" % (base[:25], index + 1)

        used.add(component_id)
        ids[group] = component_id

    components = []

    for group in ranked:
        files = sorted(group_files[group])

        components.append({
            "id": ids[group],
            "type": _component_type(group),
            "node_kind": "module",
            "label": _clean_label(group),
            "raw_label": group,
            "subtitle": "%d files · %d symbols" % (
                len(files),
                len(groups[group]),
            ),
            "node_count": len(groups[group]),
            "file_count": len(files),
            "files": files[:200],
            "folder_path": _common_folder(files),
            "file_path": "",
            "auxiliary": bool(group_auxiliary.get(group, False)),
        })

    connections = _aggregate_connections(
        graph,
        node_group,
        ranked_set,
        ids,
    )

    if not components:
        components = [{
            "id": "project",
            "type": "service",
            "node_kind": "module",
            "label": "Project",
            "raw_label": "Project",
            "subtitle": "repository",
            "node_count": 1,
            "file_count": 1,
            "files": [],
            "folder_path": "",
            "file_path": "",
            "auxiliary": False,
        }]

    return {
        "schema_version": 2,
        "view_kind": "architecture",
        "title": title,
        "path": "",
        "components": components,
        "connections": connections,
    }


def _direct_child(file_path, folder_path):
    file_p = PurePosixPath(file_path)
    folder_p = PurePosixPath(folder_path) if folder_path else PurePosixPath(".")

    try:
        relative = file_p.relative_to(folder_p)
    except ValueError:
        return None

    parts = relative.parts

    if not parts:
        return None

    if len(parts) == 1:
        return ("file", file_path)

    child_folder = str(folder_p / parts[0])

    if child_folder.startswith("./"):
        child_folder = child_folder[2:]

    return ("folder", child_folder)


def build_folder_view(graph, folder_path):
    folder_path = str(PurePosixPath(folder_path or "."))

    if folder_path == ".":
        folder_path = ""

    all_files = repository_files(graph)
    child_files = defaultdict(set)
    child_kind = {}

    for file_path in all_files:
        child = _direct_child(file_path, folder_path)

        if not child:
            continue

        kind, child_path = child
        child_kind[child_path] = kind

        if kind == "file":
            child_files[child_path].add(file_path)
        else:
            prefix = child_path.rstrip("/") + "/"

            for candidate in all_files:
                if candidate.startswith(prefix):
                    child_files[child_path].add(candidate)

    children = sorted(
        child_kind,
        key=lambda path: (
            0 if child_kind[path] == "folder" else 1,
            path.lower(),
        ),
    )

    id_map = {}
    components = []

    for child_path in children:
        kind = child_kind[child_path]
        component_id = ("%s:%s" % (kind, child_path)).replace(" ", "_")
        id_map[child_path] = component_id

        files = sorted(child_files[child_path])
        label = PurePosixPath(child_path).name

        components.append({
            "id": component_id,
            "type": "folder" if kind == "folder" else "file",
            "node_kind": kind,
            "label": _clean_label(label, 27),
            "raw_label": label,
            "subtitle": (
                "%d files" % len(files)
                if kind == "folder"
                else child_path
            ),
            "node_count": len(files),
            "file_count": len(files),
            "files": files[:200],
            "folder_path": child_path if kind == "folder" else "",
            "file_path": child_path if kind == "file" else "",
            "auxiliary": False,
        })

    symbol_bucket = {}

    for node in graph.get("nodes", []):
        file_path = node.get("file", "")
        child = _direct_child(file_path, folder_path)

        if not child:
            continue

        _, child_path = child

        if child_path in id_map:
            symbol_bucket[node["id"]] = child_path

    connections = _aggregate_connections(
        graph,
        symbol_bucket,
        set(children),
        id_map,
    )

    return {
        "schema_version": 2,
        "view_kind": "folder",
        "title": PurePosixPath(folder_path).name if folder_path else "Repository",
        "path": folder_path,
        "components": components,
        "connections": connections,
    }


def project_id(source, commit=""):
    key = "%s:%s" % (source, commit or "working")
    return hashlib.sha256(
        key.encode("utf-8", "replace")
    ).hexdigest()[:20]
