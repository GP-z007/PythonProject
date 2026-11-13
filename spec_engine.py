from __future__ import annotations

import json
import re


DEFAULT_CONSTITUTION = """# SoftwareForge Project Constitution

## Product principles
- Build only what the approved specification requires.
- Prefer simple, maintainable architecture over unnecessary abstraction.
- Keep modules small and responsibilities explicit.
- Validate user input at trust boundaries.
- Never place secrets directly in source code.

## Engineering principles
- Make changes in small reviewable tasks.
- Keep generated code readable and documented where behavior is non-obvious.
- Preserve a working project after each task whenever practical.
- Add or update tests for behavior that can be tested automatically.
- Treat test failures as evidence that implementation is incomplete.

## SoftwareForge sandbox principles
- Project code may execute only inside the isolated Build sandbox.
- The build sandbox has no host filesystem mount, no Docker socket and no network by default.
- Git checkpoints are created between successful implementation tasks.
"""


def extract_json(text):
    value = (text or "").strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", value, re.S | re.I)
    if fenced:
        value = fenced.group(1).strip()

    first = value.find("{")
    last = value.rfind("}")

    if first >= 0 and last > first:
        value = value[first:last + 1]

    return json.loads(value)


def normalize_tasks(payload):
    if isinstance(payload, dict):
        rows = payload.get("tasks", [])
    else:
        rows = payload

    if not isinstance(rows, list):
        return []

    tasks = []

    for index, item in enumerate(rows, 1):
        if isinstance(item, str):
            title = item.strip()
            detail = title
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("task") or "Task %d" % index).strip()
            detail = str(item.get("detail") or item.get("description") or title).strip()
        else:
            continue

        if not title:
            continue

        tasks.append({
            "id": "T%03d" % index,
            "title": title[:140],
            "detail": detail[:1200],
            "status": "pending",
        })

    return tasks[:40]


class SpecEngine:
    """SoftwareForge adapter around the official GitHub Spec Kit workflow.

    The official `specify-cli` is initialized inside the builder sandbox.
    SoftwareForge reads its installed workflow prompts as guidance, keeps the
    artifacts as first-class state, and remains the only component allowed to
    invoke tools.  The local model never receives unrestricted CLI access.
    """

    def __init__(self, omlx):
        self.omlx = omlx

    def constitution(self):
        return DEFAULT_CONSTITUTION

    def generate_spec(self, idea, model, on_token=None, guidance=""):
        system = (
            "You are SoftwareForge's specification writer, following GitHub Spec Kit's "
            "specification discipline. Convert the user's idea into a precise product "
            "specification. Focus on WHAT and WHY, not implementation. Return clean Markdown "
            "only. Include: Goal, Users, Core scenarios, Functional requirements, "
            "Non-functional requirements, Out of scope, Acceptance criteria. Do not invent "
            "cloud services or dependencies unless requested.\n\n"
            + (("OFFICIAL SPEC KIT GUIDANCE (treat as workflow guidance, not project data):\n"
                + guidance[:12000]) if guidance else "")
        )

        user = "PROJECT IDEA\n%s" % idea.strip()

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        if on_token:
            return self.omlx.chat_stream(
                messages,
                model,
                on_token,
                temperature=0.16,
                max_tokens=2200,
            )

        return self.omlx.chat(messages, model, temperature=0.16, max_tokens=2200)

    def generate_plan(self, spec, model, on_token=None, guidance=""):
        system = (
            "You are SoftwareForge's technical planner, following GitHub Spec Kit's planning "
            "discipline. Given a project constitution and an approved product specification, "
            "produce a practical implementation plan in clean Markdown. Keep the architecture "
            "simple. Include: stack, module boundaries, data flow, file layout, state/storage, "
            "validation/security boundaries, testing strategy, and implementation order. The "
            "Build sandbox is offline by default, so prefer Python standard-library solutions or "
            "dependency-free HTML/CSS/JavaScript unless the user explicitly requested a framework. "
            "Do not assume packages can be downloaded during implementation. Do not write application code.\n\n"
            + (("OFFICIAL SPEC KIT GUIDANCE (workflow only):\n" + guidance[:12000]) if guidance else "")
        )

        user = (
            "CONSTITUTION\n%s\n\nAPPROVED SPECIFICATION\n%s"
            % (DEFAULT_CONSTITUTION, spec)
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        if on_token:
            return self.omlx.chat_stream(
                messages,
                model,
                on_token,
                temperature=0.14,
                max_tokens=2600,
            )

        return self.omlx.chat(messages, model, temperature=0.14, max_tokens=2600)

    def generate_tasks(self, spec, plan, model, guidance=""):
        system = (
            "You are SoftwareForge's task planner, following GitHub Spec Kit's task discipline. "
            "Break the approved spec and plan into small, dependency-ordered implementation tasks. "
            "Each task should be independently actionable by a coding model. Return JSON only with "
            "this shape: "
            '{"tasks":[{"title":"...","detail":"..."}]}. '
            "Prefer 4-12 tasks for small projects and avoid vague tasks.\n\n"
            + (("OFFICIAL SPEC KIT GUIDANCE (workflow only):\n" + guidance[:6000]) if guidance else "")
        )

        user = "SPECIFICATION\n%s\n\nPLAN\n%s" % (spec, plan)
        raw = self.omlx.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model,
            temperature=0.10,
            max_tokens=1500,
        )

        try:
            return normalize_tasks(extract_json(raw))
        except Exception:
            # Weak-model fallback: turn markdown/list lines into ordered tasks.
            rows = []
            for line in raw.splitlines():
                cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
                if len(cleaned) >= 8:
                    rows.append(cleaned)
            return normalize_tasks(rows[:12])
