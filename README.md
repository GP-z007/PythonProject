# SoftwareForge — Analyze + Build

SoftwareForge is a Python / CustomTkinter desktop application that turns source code into a visual workspace and uses local AI without sending an entire repository to the model.

```text
SoftwareForge
├── Analyze
│   ├── Project
│   ├── Canvas
│   └── Agent
└── Build
    ├── Project
    ├── Forge
    └── Live Canvas
```

## Analyze

Analyze treats every repository as untrusted data.

- GitHub or local-folder input
- static analysis only
- interactive n8n/ComfyUI-inspired Canvas
- nested folder/subgraph navigation
- draggable nodes and groups
- curved relationships, Arrange, Fit and Flow
- floating syntax-highlighted source viewer
- **Analyze Agent** with oMLX + BGE-M3 + Weaviate RAG
- Markdown chat, streaming, compact composer and floating Sources/source preview
- Canvas → **Ask Agent** context handoff

The Analyze sandbox never executes repository code.

## Build

Build uses one local coding assistant, **Forge**, instead of a swarm of agents.

```text
idea
  ↓
Spec Kit workflow guidance
  ↓
specification
  ↓
plan
  ↓
tasks
  ↓
isolated implementation
  ↓
tests / convergence
  ↓
live Canvas
```

### Real GitHub Spec Kit integration

The Build image pins the official Python package:

```text
specify-cli==1.0.4
```

During `docker build`, the image uses the official `specify` CLI with its **generic / bring-your-own-agent integration** to create a pinned Spec Kit scaffold. The generated `.specify/` and command guidance are baked into the trusted image. Every disposable Build container copies that scaffold into `/work/project` while remaining `network_mode=none` at runtime.

SoftwareForge—not the model—controls the Spec Kit tooling. Forge reads the installed Spec Kit workflow guidance for specification, planning and task generation, while tool execution remains behind SoftwareForge's own controlled interfaces.

The approved artifacts are kept in both SoftwareForge and conventional project locations:

```text
.softwareforge/
├── constitution.md
├── spec.md
├── plan.md
├── tasks.md
└── spec-kit/...

.specify/
└── ... official Spec Kit scaffold ...

specs/001-softwareforge/
├── spec.md
├── plan.md
└── tasks.md
```

## Live Build Canvas

After every implementation task:

1. Forge applies bounded validated file writes inside the builder container.
2. Git creates a checkpoint.
3. SoftwareForge statically analyzes the generated project.
4. Canvas revision increments.
5. New modules appear while the project is being built.

Existing Canvas positions are retained where possible so the graph does not jump unnecessarily.

## Project Brain and memory

Weaviate has two logical data layers:

### `SoftwareForgeChunk`

Vector/searchable source knowledge:

- project key
- file path
- language
- module
- symbol
- source line range
- focused code content
- BGE-M3 vector

Chunking prefers functions/classes/interfaces and uses bounded line windows only as a fallback.

### `SoftwareForgeMemory`

Compact durable project memory:

- Analyze Agent question/answer memory
- cited source locations
- specification
- plan
- tasks
- Build task events / changed files
- verification result

The model never receives all stored memory. SoftwareForge retrieves only a tiny relevant subset.

### Retrieval efficiency

Fast mode remains bounded while preserving a longer conversation:

- exact file/symbol-looking queries prefer lexical retrieval first
- up to ~12 candidates
- ~5 final evidence windows
- ~12k source-character budget
- compact architecture capsule
- the last 8 conversation turns, up to 4k characters each
- query embeddings and Weaviate search results are cached

Deep mode uses up to ~20k source characters and ~8 evidence windows, with the
same eight-turn conversation window.

Forge task execution is also bounded: a compact project tree, about three task-relevant files, and shortened spec/plan excerpts rather than a full-project dump.

## Chat UI

Agent and Forge use the same calm ChatGPT-inspired interaction model while remaining SoftwareForge-branded:

- dark charcoal surfaces
- larger readable body text
- Markdown headings/lists/inline code/fenced code
- compact centered rounded composer
- `Enter` sends, `Shift+Enter` inserts a newline
- streaming answers when oMLX supports it
- visible process states such as `Searching project…`, `Reading relevant code…`, `Thinking…`, and `Generating…`
- compact inline citations plus a floating Sources panel
- source preview inside the floating panel
- explicit **Open in Canvas** action

No ChatGPT/OpenAI logo or proprietary font files are bundled.

## Sidebar

The persistent application sidebar physically changes width between approximately 220 px and 60 px. Collapse state survives page switches. In collapsed mode it keeps only the navigation/status icons so the released space belongs to the main workspace.

## Security

### Analyze sandbox

Repositories are potentially malicious.

- non-root UID 10001
- read-only root filesystem
- all Linux capabilities dropped
- `no-new-privileges`
- CPU / RAM / PID limits
- no Docker socket
- no host source bind mount
- GitHub clone network is removed immediately after clone
- repository code is not executed

### Build sandbox

Build intentionally executes **generated** code for testing, but only inside a different disposable container.

- non-root UID 10001
- read-only root filesystem
- writable bounded `/work` tmpfs only
- no Docker socket
- no host project bind mount
- network disabled at runtime
- all capabilities dropped
- `no-new-privileges`
- CPU / RAM / PID limits
- bounded generated-file count/size/path validation
- Git checkpoints between tasks

Docker is defense in depth and should not be described as equivalent to a full VM security boundary.

## oMLX

SoftwareForge expects the OpenAI-compatible oMLX server on port `8000`.

Connection order:

1. `SOFTWAREFORGE_OMLX_URL`
2. `http://host.docker.internal:8000/v1`
3. `http://127.0.0.1:8000/v1`

Load:

- a chat model such as your Qwen model
- an embedding model such as `bge-m3-mlx-fp16`

## Install

From the project folder:

```bash
pip install -r requirements.txt
```

The main requirements file conditionally includes Spec Kit only on Python 3.11+. Your desktop app can still run in the existing Python 3.9 devcontainer because the **required pinned Spec Kit installation lives in the Python 3.13 builder image**.

Build Analyze:

```bash
docker build \
  -f Dockerfile.analyzer \
  -t softwareforge-analyzer:latest \
  .
```

Build Forge / Spec Kit:

```bash
docker build \
  -f Dockerfile.builder \
  -t softwareforge-builder:latest \
  .
```

The second build needs internet access once so the image can install the pinned package and generate the official Spec Kit scaffold. Runtime Build containers remain network-disabled.

Start Weaviate:

```bash
docker compose up -d
```

Verify:

```bash
pytest -q
python smoke_security.py
```

Launch:

```bash
python app.py
```

## Current automated validation

The included unit suite covers graph normalization/folder navigation, symbol-aware chunking, model classification, context compaction, Spec/Task parsing and Forge file-path validation. `smoke_security.py` additionally checks both Docker sandbox policies and confirms the Build image contains usable Spec Kit guidance.
