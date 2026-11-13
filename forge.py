from __future__ import annotations

from pathlib import PurePosixPath
import json
import re

from spec_engine import SpecEngine, extract_json


TEXT_FILE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".html", ".css", ".scss",
    ".md", ".txt", ".toml", ".yaml", ".yml", ".sh", ".bash", ".sql", ".java",
    ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".kts",
}

MAX_MODEL_FILES = 12
MAX_MODEL_BYTES = 800 * 1024


def validate_file_payload(payload):
    if not isinstance(payload, dict):
        raise RuntimeError("Forge returned an invalid file payload.")

    rows = payload.get("files")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Forge returned no project files.")
    if len(rows) > MAX_MODEL_FILES:
        raise RuntimeError("Forge returned too many project files.")

    validated = []
    total_bytes = 0
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise RuntimeError("Forge returned a file without valid content.")

        path = str(item.get("path", "")).replace("\\", "/").strip()
        pure = PurePosixPath(path)
        if (
            not path
            or "\x00" in path
            or pure.is_absolute()
            or ".." in pure.parts
            or path.startswith("~")
            or re.match(r"^[A-Za-z]:", path)
            or len(path) > 420
        ):
            raise RuntimeError("Forge returned an unsafe project path.")

        content = item["content"]
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > MAX_MODEL_BYTES:
            raise RuntimeError("Forge returned too much project content.")
        validated.append({"path": str(pure), "content": content})

    return validated


class ForgeService:
    def __init__(self, omlx):
        self.omlx = omlx
        self.spec_engine = SpecEngine(omlx)

    @staticmethod
    def slugify(value):
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
        return (slug[:48] or "softwareforge-project")

    @staticmethod
    def task_markdown(tasks):
        rows = ["# Implementation Tasks", ""]
        for task in tasks:
            mark = "x" if task.get("status") == "done" else " "
            rows.append("- [%s] **%s — %s**" % (mark, task["id"], task["title"]))
            if task.get("detail") and task["detail"] != task["title"]:
                rows.append("  %s" % task["detail"])
        return "\n".join(rows)

    def generate_spec(self, idea, model, on_token=None, guidance=""):
        return self.spec_engine.generate_spec(
            idea, model, on_token=on_token, guidance=guidance
        )

    def generate_plan(self, spec, model, on_token=None, guidance=""):
        return self.spec_engine.generate_plan(
            spec, model, on_token=on_token, guidance=guidance
        )

    def generate_tasks(self, spec, plan, model, guidance=""):
        return self.spec_engine.generate_tasks(
            spec, plan, model, guidance=guidance
        )

    def _relevant_files(self, builder, task, limit=5):
        files = builder.list_files()
        terms = [
            token.lower()
            for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$.-]{2,}", task.get("title", "") + " " + task.get("detail", ""))
        ]
        terms = list(dict.fromkeys(terms))[:18]
        ranked = []

        for path in files:
            suffix = PurePosixPath(path).suffix.lower()
            if suffix and suffix not in TEXT_FILE_EXTENSIONS:
                continue
            low = path.lower()
            score = sum(2 for term in terms if term in low)
            if path in ("README.md", "pyproject.toml", "package.json", "requirements.txt"):
                score += 1
            ranked.append((score, path))

        ranked.sort(key=lambda pair: (-pair[0], pair[1]))
        chosen = [path for score, path in ranked if score > 0][:limit]

        if len(chosen) < min(3, limit):
            for _, path in ranked:
                if path not in chosen:
                    chosen.append(path)
                if len(chosen) >= min(3, limit):
                    break

        return chosen[:limit]

    def _task_context(self, builder, task, spec, plan):
        tree = builder.tree_text(max_files=110)
        files = self._relevant_files(builder, task, limit=3)
        blocks = []
        used = 0

        for path in files:
            try:
                content = builder.read_source(path)
            except Exception:
                continue

            excerpt = content[:2800]
            if used + len(excerpt) > 6200:
                excerpt = excerpt[: max(800, 6200 - used)]
            if not excerpt:
                continue
            blocks.append("FILE %s\n%s" % (path, excerpt))
            used += len(excerpt)
            if used >= 6200:
                break

        return (
            "TASK\n%s — %s\n%s\n\n"
            "PROJECT TREE\n%s\n\n"
            "RELEVANT EXISTING FILES\n%s\n\n"
            "SPEC EXCERPT\n%s\n\n"
            "PLAN EXCERPT\n%s"
            % (
                task.get("id", "task"),
                task.get("title", ""),
                task.get("detail", ""),
                tree or "(empty project)",
                "\n\n".join(blocks) if blocks else "(no existing source yet)",
                spec[:3200],
                plan[:3200],
            )
        )

    def implement_task(self, builder, task, spec, plan, model):
        system = (
            "You are Forge, SoftwareForge's coding model. Implement ONE approved task inside an "
            "isolated build sandbox. Return JSON only. Never include shell commands. Never edit "
            "files outside the project. Prefer complete file contents because SoftwareForge applies "
            "changes transactionally. Shape: "
            '{"summary":"...","files":[{"path":"relative/path","content":"complete file content"}]}. '
            "Modify the fewest files needed. Preserve working existing behavior. The sandbox has no "
            "network, so prefer dependency-free files and standard-library code. For a website, use "
            "plain HTML/CSS/JavaScript unless the approved plan explicitly requires a framework. "
            "Do not add package dependencies unless the approved plan clearly requires them."
        )

        context = self._task_context(builder, task, spec, plan)
        raw = self.omlx.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": context},
            ],
            model,
            temperature=0.08,
            max_tokens=2200,
        )

        payload = extract_json(raw)
        rows = validate_file_payload(payload)
        applied = []

        for item in rows:
            path = str(item.get("path", "")).strip()
            content = item.get("content")
            builder.write_file(path, content)
            applied.append(path)

        if not applied:
            raise RuntimeError("Forge returned no valid project file changes for %s." % task.get("id", "task"))

        return {
            "summary": str(payload.get("summary", "Implemented %s" % task.get("title", "task")))[:1200],
            "files": applied,
        }

    def convergence_report(self, spec, plan, tasks, test_result, model):
        task_text = self.task_markdown(tasks)
        system = (
            "You are SoftwareForge's verifier. Compare the approved specification, plan, completed "
            "tasks and automated test result. Return concise Markdown with: Verification, Covered, "
            "Remaining gaps, and Result. If there is no evidence of a missing requirement, say "
            "CONVERGED. Do not invent test results."
        )

        user = (
            "SPEC\n%s\n\nPLAN\n%s\n\nTASKS\n%s\n\nTEST RESULT\n%s"
            % (spec[:6500], plan[:6500], task_text[:5000], test_result.get("output", "")[-5000:])
        )

        return self.omlx.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model,
            temperature=0.10,
            max_tokens=900,
        )
