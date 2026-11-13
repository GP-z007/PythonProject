from __future__ import annotations

from collections import OrderedDict, defaultdict
from pathlib import PurePosixPath
import json
import os
import re
import uuid

import requests


CLASS_NAME = "SoftwareForgeChunk"
MEMORY_CLASS_NAME = "SoftwareForgeMemory"
MAX_INDEX_FILES = 500
MAX_CHUNKS = 2600
CHUNK_LINES = 58
CHUNK_OVERLAP = 8
EMBED_CODE_CHARS = 2800
DEFAULT_CONTEXT_BUDGET = 20000
DEFAULT_SOURCE_LIMIT = 6


def language_for_path(path):
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "jsx",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".sh": "shell",
        ".bash": "shell",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".sql": "sql",
    }.get(suffix, "text")


def module_for_path(path):
    parts = list(PurePosixPath(path).parts)

    while parts and parts[0].lower() in {
        "src", "app", "source", "lib", "server", "backend", "frontend",
    }:
        parts = parts[1:]

    return parts[0] if len(parts) > 1 else "Core"


def detect_symbol(lines):
    patterns = (
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
        r"^\s*def\s+([A-Za-z_][\w]*)",
        r"^\s*class\s+([A-Za-z_][\w]*)",
        r"^\s*func\s+([A-Za-z_][\w]*)",
    )

    for line in lines[:22]:
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                return match.group(1)

    return ""


def code_signals(lines, limit=18):
    """Extract cheap structural hints so embeddings see more signal than boilerplate."""
    patterns = (
        r"^\s*(?:import|from|require\b|use\b|using\b).*$",
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_$][\w$]*",
        r"^\s*(?:export\s+)?class\s+[A-Za-z_$][\w$]*",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=",
        r"^\s*(?:def|class|func)\s+[A-Za-z_][\w]*",
        r"^\s*(?:interface|type|enum|struct|trait)\s+[A-Za-z_$][\w$]*",
    )

    signals = []

    for line in lines:
        stripped = line.strip()
        if not stripped or len(stripped) > 220:
            continue

        if any(re.search(pattern, line) for pattern in patterns):
            signals.append(stripped)

        if len(signals) >= limit:
            break

    return signals


def _symbol_starts(lines):
    patterns = (
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?",
        r"^\s*(?:def|class)\s+([A-Za-z_][\w]*)",
        r"^\s*func\s+([A-Za-z_][\w]*)",
        r"^\s*(?:interface|type|enum|struct|trait)\s+([A-Za-z_$][\w$]*)",
    )

    rows = []
    for index, line in enumerate(lines):
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                rows.append((index, match.group(1)))
                break
    return rows


def chunk_source(path, source):
    """Prefer symbol-sized chunks, falling back to bounded line windows.

    The model gets denser evidence because a function/class is usually kept intact
    instead of being split across arbitrary overlapping 58-line windows.
    """
    lines = source.splitlines()
    if not lines:
        return []

    starts = _symbol_starts(lines)
    chunks = []

    # Keep a small file preamble (imports/constants) when symbols start later.
    if starts and starts[0][0] > 0:
        end = min(starts[0][0], 48)
        if end > 0:
            chunks.append({
                "path": path,
                "language": language_for_path(path),
                "module": module_for_path(path),
                "symbol": "",
                "start_line": 1,
                "end_line": end,
                "content": "\n".join(lines[:end]),
            })

    if starts:
        for position, (start, symbol) in enumerate(starts):
            natural_end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            cursor = start

            while cursor < natural_end:
                end = min(natural_end, cursor + 86)
                block = lines[cursor:end]
                chunks.append({
                    "path": path,
                    "language": language_for_path(path),
                    "module": module_for_path(path),
                    "symbol": symbol,
                    "start_line": cursor + 1,
                    "end_line": end,
                    "content": "\n".join(block),
                })
                if end >= natural_end:
                    break
                cursor = max(cursor + 1, end - 8)

        return chunks

    # Files without recognizable symbols still use compact overlapping windows.
    start = 0
    while start < len(lines):
        end = min(len(lines), start + CHUNK_LINES)
        block = lines[start:end]
        chunks.append({
            "path": path,
            "language": language_for_path(path),
            "module": module_for_path(path),
            "symbol": detect_symbol(block),
            "start_line": start + 1,
            "end_line": end,
            "content": "\n".join(block),
        })
        if end >= len(lines):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)

    return chunks


def embedding_text(chunk):
    lines = chunk.get("content", "").splitlines()
    signals = code_signals(lines)
    compact_code = chunk.get("content", "")[:EMBED_CODE_CHARS]

    return (
        "Path: {path}\n"
        "Module: {module}\n"
        "Language: {language}\n"
        "Primary symbol: {symbol}\n"
        "Structural signals:\n{signals}\n\n"
        "Code excerpt:\n{code}"
    ).format(
        path=chunk.get("path", ""),
        module=chunk.get("module", ""),
        language=chunk.get("language", ""),
        symbol=chunk.get("symbol", ""),
        signals="\n".join(signals[:18]),
        code=compact_code,
    )


def query_terms(query):
    stop = {
        "this", "that", "with", "from", "where", "what", "when", "which",
        "does", "have", "into", "about", "project", "code", "file", "files",
        "please", "explain", "show", "tell", "change", "would", "could",
        "should", "there", "their", "they", "them", "then", "than", "how",
    }

    terms = []
    for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$.-]{2,}", query or ""):
        low = token.lower()
        if low not in stop and low not in terms:
            terms.append(low)

    return terms[:18]


def compact_result(item, query, max_lines=30):
    content = item.get("content", "") or ""
    lines = content.splitlines()

    if not lines:
        return dict(item)

    terms = query_terms(query)
    symbol = (item.get("symbol") or "").lower()
    best_index = 0
    best_score = -1

    for index, line in enumerate(lines):
        low = line.lower()
        score = 0

        if symbol and symbol in low:
            score += 5

        for term in terms:
            if term in low:
                score += 2

        if score > best_score:
            best_score = score
            best_index = index

    if best_score <= 0:
        best_index = 0

    half = max_lines // 2
    start = max(0, best_index - half)
    end = min(len(lines), start + max_lines)
    start = max(0, end - max_lines)

    result = dict(item)
    original_start = int(item.get("start_line", 1) or 1)
    result["content"] = "\n".join(lines[start:end])
    result["start_line"] = original_start + start
    result["end_line"] = original_start + end - 1
    return result


def budget_results(results, query, char_budget=DEFAULT_CONTEXT_BUDGET, limit=DEFAULT_SOURCE_LIMIT):
    compacted = []
    used = 0

    for item in results[: max(limit * 2, limit)]:
        compact = compact_result(item, query)
        content = compact.get("content", "")
        overhead = 180
        needed = len(content) + overhead

        if compacted and used + needed > char_budget:
            continue

        if needed > char_budget and not compacted:
            compact["content"] = content[: max(1200, char_budget - overhead)]
            needed = len(compact["content"]) + overhead

        compacted.append(compact)
        used += needed

        if len(compacted) >= limit or used >= char_budget:
            break

    return compacted


class OMLXClient:
    def __init__(self, base_url=None, api_key=None):
        configured = base_url or os.environ.get("SOFTWAREFORGE_OMLX_URL")

        self.candidates = [
            configured,
            "http://host.docker.internal:1234/v1",
            "http://127.0.0.1:1234/v1",
        ]

        self.candidates = [
            value.rstrip("/")
            for value in self.candidates
            if value
        ]

        self.candidates = list(dict.fromkeys(self.candidates))
        self.base_url = self.candidates[0]
        self.api_key = (
            api_key
            or os.environ.get("SOFTWAREFORGE_OMLX_API_KEY")
            or "omlx"
        )

        self._query_embedding_cache = OrderedDict()
        self._cache_limit = 96

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.api_key,
        }

    def discover_base_url(self):
        last_error = None

        for candidate in self.candidates:
            try:
                response = requests.get(
                    candidate + "/models",
                    headers=self._headers(),
                    timeout=3,
                )
                response.raise_for_status()
                self.base_url = candidate
                return candidate
            except Exception as exc:
                last_error = exc

        raise RuntimeError(
            "oMLX was not reachable on port 8000. "
            "Tried host.docker.internal and localhost. %s"
            % (last_error or "")
        )

    def models(self):
        self.discover_base_url()

        response = requests.get(
            self.base_url + "/models",
            headers=self._headers(),
            timeout=8,
        )
        response.raise_for_status()

        payload = response.json()
        return [
            item.get("id")
            for item in payload.get("data", [])
            if item.get("id")
        ]

    @staticmethod
    def classify_models(models):
        embedding = []
        reranker = []
        chat = []

        for model in models:
            low = model.lower()

            if "rerank" in low:
                reranker.append(model)
            elif any(token in low for token in (
                "embed", "embedding", "bge-", "bge_", "e5-", "gte-",
                "nomic-embed", "snowflake-arctic-embed",
            )):
                embedding.append(model)
            else:
                chat.append(model)

        return {
            "chat": chat,
            "embedding": embedding,
            "reranker": reranker,
        }

    @classmethod
    def choose_models(cls, models):
        groups = cls.classify_models(models)

        return {
            "chat": groups["chat"][0] if groups["chat"] else (models[0] if models else ""),
            "embedding": groups["embedding"][0] if groups["embedding"] else "",
            "reranker": groups["reranker"][0] if groups["reranker"] else "",
        }

    def embeddings(self, inputs, model):
        if not model:
            raise RuntimeError(
                "No embedding model is selected. Load an embedding model in oMLX."
            )

        response = requests.post(
            self.base_url + "/embeddings",
            headers=self._headers(),
            json={
                "model": model,
                "input": inputs,
            },
            timeout=90,
        )
        response.raise_for_status()

        data = response.json().get("data", [])
        data.sort(key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in data]

    def embed_query(self, query, model):
        key = (model, query.strip())

        if key in self._query_embedding_cache:
            vector = self._query_embedding_cache.pop(key)
            self._query_embedding_cache[key] = vector
            return vector

        vector = self.embeddings([query], model)[0]
        self._query_embedding_cache[key] = vector

        while len(self._query_embedding_cache) > self._cache_limit:
            self._query_embedding_cache.popitem(last=False)

        return vector

    def chat(self, messages, model, temperature=0.18, max_tokens=1050):
        if not model:
            raise RuntimeError("No chat model is selected.")

        response = requests.post(
            self.base_url + "/chat/completions",
            headers=self._headers(),
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
                "chat_template_kwargs": {
                    "enable_thinking": False,
                },
            },
            timeout=180,
        )
        response.raise_for_status()

        payload = response.json()
        choices = payload.get("choices", [])

        if not choices:
            raise RuntimeError("oMLX returned no chat completion.")

        message = choices[0].get("message") or {}
        return (
            message.get("content")
            or message.get("text")
            or message.get("reasoning_content")
            or ""
        )

    def chat_stream(self, messages, model, on_token, temperature=0.18, max_tokens=1050):
        """Stream visible answer tokens; callers can fall back to chat()."""
        if not model:
            raise RuntimeError("No chat model is selected.")

        response = requests.post(
            self.base_url + "/chat/completions",
            headers=self._headers(),
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
                "chat_template_kwargs": {
                    "enable_thinking": False,
                },
            },
            timeout=(10, 180),
            stream=True,
        )
        response.raise_for_status()

        parts = []
        reasoning_parts = []
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue

            if line.startswith("data:"):
                line = line[5:].strip()

            if line == "[DONE]":
                break

            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue

            choices = payload.get("choices", [])
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}
            token = (
                delta.get("content")
                or delta.get("text")
                or choice.get("text")
                or ""
            )
            if token:
                parts.append(token)
                on_token(token)
            else:
                reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or ""
                )
                if reasoning:
                    reasoning_parts.append(reasoning)

        answer = "".join(parts)
        if not answer:
            answer = "".join(reasoning_parts)

        if not answer:
            answer = self.chat(
                messages,
                model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if answer:
                on_token(answer)

        if not answer:
            raise RuntimeError(
                "LM Studio returned no chat content from streaming or fallback completion."
            )

        return answer


class WeaviateBrain:
    def __init__(self, http_url=None):
        self.http_url = (
            http_url
            or os.environ.get("SOFTWAREFORGE_WEAVIATE_URL")
            or "http://127.0.0.1:8080"
        ).rstrip("/")

        self._search_cache = OrderedDict()
        self._search_cache_limit = 64

    def ready(self):
        try:
            response = requests.get(
                self.http_url + "/v1/.well-known/ready",
                timeout=2,
            )
            return response.ok
        except Exception:
            return False

    def ensure_schema(self):
        response = requests.get(
            self.http_url + "/v1/schema/%s" % CLASS_NAME,
            timeout=5,
        )

        if response.status_code == 200:
            return

        schema = {
            "class": CLASS_NAME,
            "vectorizer": "none",
            "properties": [
                {"name": "project_key", "dataType": ["text"]},
                {"name": "path", "dataType": ["text"]},
                {"name": "language", "dataType": ["text"]},
                {"name": "module", "dataType": ["text"]},
                {"name": "symbol", "dataType": ["text"]},
                {"name": "start_line", "dataType": ["int"]},
                {"name": "end_line", "dataType": ["int"]},
                {"name": "content", "dataType": ["text"]},
            ],
        }

        response = requests.post(
            self.http_url + "/v1/schema",
            json=schema,
            timeout=10,
        )

        if response.status_code not in (200, 201):
            raise RuntimeError(
                "Could not create Weaviate schema: %s"
                % response.text[:800]
            )

    def ensure_memory_schema(self):
        response = requests.get(
            self.http_url + "/v1/schema/%s" % MEMORY_CLASS_NAME,
            timeout=5,
        )
        if response.status_code == 200:
            return

        schema = {
            "class": MEMORY_CLASS_NAME,
            "vectorizer": "none",
            "properties": [
                {"name": "project_key", "dataType": ["text"]},
                {"name": "kind", "dataType": ["text"]},
                {"name": "memory_key", "dataType": ["text"]},
                {"name": "path", "dataType": ["text"]},
                {"name": "content", "dataType": ["text"]},
                {"name": "created_at", "dataType": ["text"]},
            ],
        }
        response = requests.post(
            self.http_url + "/v1/schema",
            json=schema,
            timeout=10,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                "Could not create SoftwareForge memory schema: %s"
                % response.text[:800]
            )

    def remember(self, project_key, kind, memory_key, content, path="", created_at=""):
        """Store a compact durable project memory without embedding it again.

        Code remains in the vector collection; specs, decisions, conversations and
        source-location notes live here and are retrieved lexically with tiny limits.
        """
        if not self.ready() or not project_key or not content:
            return False
        self.ensure_memory_schema()
        object_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            "softwareforge-memory:%s:%s:%s" % (project_key, kind, memory_key),
        ))
        payload = {
            "class": MEMORY_CLASS_NAME,
            "id": object_id,
            "properties": {
                "project_key": str(project_key),
                "kind": str(kind or "note")[:80],
                "memory_key": str(memory_key or object_id)[:240],
                "path": str(path or "")[:1200],
                "content": str(content)[:16000],
                "created_at": str(created_at or "")[:80],
            },
        }

        # PUT gives us idempotent memory for stable keys such as spec/plan/tasks.
        response = requests.put(
            self.http_url + "/v1/objects/%s/%s" % (MEMORY_CLASS_NAME, object_id),
            json=payload,
            timeout=12,
        )
        if response.status_code in (200, 201, 204):
            return True

        # If this is the first write for the deterministic id, create it.
        response = requests.post(
            self.http_url + "/v1/objects",
            json=payload,
            timeout=12,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError("Weaviate memory write failed: %s" % response.text[:600])
        return True

    def memory_search(self, project_key, query, limit=2):
        if not self.ready() or not project_key or not str(query or "").strip():
            return []
        try:
            self.ensure_memory_schema()
            where = (
                'where:{path:["project_key"],operator:Equal,valueText:%s}'
                % self._escape_graphql(project_key)
            )
            gql = """
            {
              Get {
                %s(%s,bm25:{query:%s},limit:%d) {
                  kind
                  memory_key
                  path
                  content
                  created_at
                  _additional { score }
                }
              }
            }
            """ % (
                MEMORY_CLASS_NAME,
                where,
                self._escape_graphql(query),
                int(max(1, min(6, limit))),
            )
            response = requests.post(
                self.http_url + "/v1/graphql",
                json={"query": gql},
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                return []
            return (
                payload.get("data", {})
                .get("Get", {})
                .get(MEMORY_CLASS_NAME, [])
            )
        except Exception:
            return []

    @staticmethod
    def _escape_graphql(value):
        return json.dumps(str(value))

    def project_count(self, project_key):
        if not self.ready():
            return 0

        where = (
            'where:{path:["project_key"],operator:Equal,valueText:%s}'
            % self._escape_graphql(project_key)
        )

        gql = """
        {
          Aggregate {
            %s(%s) {
              meta { count }
            }
          }
        }
        """ % (CLASS_NAME, where)

        try:
            response = requests.post(
                self.http_url + "/v1/graphql",
                json={"query": gql},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()

            if payload.get("errors"):
                return 0

            rows = (
                payload
                .get("data", {})
                .get("Aggregate", {})
                .get(CLASS_NAME, [])
            )

            if not rows:
                return 0

            return int(rows[0].get("meta", {}).get("count", 0) or 0)
        except Exception:
            return 0

    def insert_batch(self, objects):
        if not objects:
            return

        response = requests.post(
            self.http_url + "/v1/batch/objects",
            json={"objects": objects},
            timeout=60,
        )

        if response.status_code not in (200, 201):
            raise RuntimeError(
                "Weaviate batch insert failed: %s"
                % response.text[:1000]
            )

    def index_chunks(
        self,
        project_key,
        chunks,
        omlx,
        embedding_model,
        progress=None,
    ):
        self.ensure_schema()

        batch_size = 24
        inserted = 0

        for offset in range(0, len(chunks), batch_size):
            batch = chunks[offset: offset + batch_size]
            texts = [embedding_text(chunk) for chunk in batch]
            vectors = omlx.embeddings(texts, embedding_model)
            objects = []

            for chunk, vector in zip(batch, vectors):
                object_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "%s:%s:%s:%s"
                        % (
                            project_key,
                            chunk["path"],
                            chunk["start_line"],
                            chunk["end_line"],
                        ),
                    )
                )

                properties = dict(chunk)
                properties["project_key"] = project_key

                objects.append({
                    "class": CLASS_NAME,
                    "id": object_id,
                    "properties": properties,
                    "vector": vector,
                })

            self.insert_batch(objects)
            inserted += len(objects)

            if progress:
                progress(inserted, len(chunks))

        self._search_cache.clear()

    def search(self, project_key, query, vector=None, limit=14):
        cache_key = (
            project_key,
            query,
            bool(vector is not None),
            int(limit),
        )

        if cache_key in self._search_cache:
            rows = self._search_cache.pop(cache_key)
            self._search_cache[cache_key] = rows
            return [dict(item) for item in rows]

        where = (
            'where:{path:["project_key"],operator:Equal,valueText:%s}'
            % self._escape_graphql(project_key)
        )

        if vector is not None:
            search_clause = (
                "hybrid:{query:%s,vector:%s,alpha:0.66}"
                % (
                    self._escape_graphql(query),
                    json.dumps(vector),
                )
            )
        else:
            search_clause = (
                "bm25:{query:%s}"
                % self._escape_graphql(query)
            )

        gql = """
        {
          Get {
            %s(%s,%s,limit:%d) {
              path
              language
              module
              symbol
              start_line
              end_line
              content
              _additional { score }
            }
          }
        }
        """ % (
            CLASS_NAME,
            where,
            search_clause,
            int(limit),
        )

        response = requests.post(
            self.http_url + "/v1/graphql",
            json={"query": gql},
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()

        if payload.get("errors"):
            raise RuntimeError(
                "Weaviate query failed: %s"
                % payload["errors"][0].get(
                    "message",
                    "unknown error",
                )
            )

        rows = (
            payload
            .get("data", {})
            .get("Get", {})
            .get(CLASS_NAME, [])
        )

        self._search_cache[cache_key] = [dict(item) for item in rows]

        while len(self._search_cache) > self._search_cache_limit:
            self._search_cache.popitem(last=False)

        return rows

    def optimized_search(
        self,
        project_key,
        query,
        vector=None,
        selected_paths=None,
        candidates=14,
        limit=DEFAULT_SOURCE_LIMIT,
        context_budget=DEFAULT_CONTEXT_BUDGET,
    ):
        rows = self.search(
            project_key,
            query,
            vector=vector,
            limit=candidates,
        )

        terms = query_terms(query)
        selected_paths = set(selected_paths or [])
        ranked = []

        for position, item in enumerate(rows):
            path = item.get("path", "")
            symbol = item.get("symbol", "")
            module = item.get("module", "")
            haystack = "%s %s %s" % (path, symbol, module)
            low = haystack.lower()

            score = max(0.0, (candidates - position) / float(candidates))

            if path in selected_paths:
                score += 3.0

            for term in terms:
                if term in path.lower():
                    score += 1.2
                if term in symbol.lower():
                    score += 1.5
                if term in module.lower():
                    score += 0.7
                if term in low:
                    score += 0.15

            ranked.append((score, item))

        ranked.sort(key=lambda pair: pair[0], reverse=True)

        chosen = []
        per_file = defaultdict(int)

        for _, item in ranked:
            path = item.get("path", "")

            if per_file[path] >= 2:
                continue

            chosen.append(item)
            per_file[path] += 1

            if len(chosen) >= limit * 2:
                break

        return budget_results(
            chosen,
            query,
            char_budget=context_budget,
            limit=limit,
        )


class ProjectIndexer:
    def __init__(self, brain, omlx):
        self.brain = brain
        self.omlx = omlx

    def build_chunks(self, graph, sandbox, progress=None):
        files = sorted({
            node.get("file")
            for node in graph.get("nodes", [])
            if node.get("file")
        })[:MAX_INDEX_FILES]

        all_chunks = []

        for index, path in enumerate(files):
            try:
                source = sandbox.read_source(path)
            except Exception:
                continue

            chunks = chunk_source(path, source)
            remaining = MAX_CHUNKS - len(all_chunks)

            if remaining <= 0:
                break

            all_chunks.extend(chunks[:remaining])

            if progress:
                progress(
                    "Reading source",
                    index + 1,
                    len(files),
                )

        return all_chunks
