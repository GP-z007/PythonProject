from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import io
import json
import os
from pathlib import PurePosixPath
import re
import tarfile
import time

import docker
from docker.errors import DockerException

from analyzer import normalize_graph


BUILDER_IMAGE = "softwareforge-builder:latest"
MAX_SOURCE_BYTES = 600 * 1024
MAX_WRITE_BYTES = 800 * 1024
MAX_FILES = 1200


class BuilderError(RuntimeError):
    pass


def _safe_relative(path):
    value = str(path or "").replace("\\", "/").strip()
    pure = PurePosixPath(value)

    if (
        not value
        or "\x00" in value
        or pure.is_absolute()
        or ".." in pure.parts
        or value.startswith("~")
        or re.match(r"^[A-Za-z]:", value)
        or len(value) > 420
    ):
        raise BuilderError("Unsafe project path.")

    return str(pure)


class BuilderSandbox:
    """Writable, disposable build sandbox.

    Unlike Analyze, Build intentionally executes generated project code for tests.
    It still gets no host mounts, no Docker socket, no capabilities and no network.
    """

    def __init__(self):
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as exc:
            raise BuilderError("Docker is not available.") from exc

        self.session = None
        self.project_name = ""

    def image_ready(self):
        try:
            self.client.images.get(BUILDER_IMAGE)
            return True
        except Exception:
            return False

    def session_active(self):
        if self.session is None:
            return False

        try:
            self.session.reload()
            return self.session.status == "running"
        except Exception:
            return False

    def start(self, project_name="softwareforge-project"):
        if self.session_active():
            return

        if not self.image_ready():
            raise BuilderError(
                "Build sandbox image is missing. Build it with: "
                "docker build -f Dockerfile.builder -t softwareforge-builder:latest ."
            )

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_name or "project")[:80]
        safe_name = safe_name.strip("-.") or "project"

        self.session = self.client.containers.run(
            BUILDER_IMAGE,
            command=["sleep", "infinity"],
            detach=True,
            auto_remove=False,
            network_mode="none",
            read_only=True,
            user="10001:10001",
            privileged=False,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=256,
            mem_limit="2g",
            memswap_limit="2g",
            nano_cpus=3000000000,
            init=True,
            tmpfs={
                "/work": "rw,nosuid,nodev,size=1400m,uid=10001,gid=10001",
                "/tmp": "rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001",
            },
            environment={
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONNOUSERSITE": "1",
                "CI": "1",
            },
        )

        self.project_name = safe_name

        try:
            self._exec(
                [
                    "python",
                    "-c",
                    (
                        "from pathlib import Path;"
                        "p=Path('/work/project');p.mkdir(parents=True,exist_ok=True)"
                    ),
                ],
                "Create build workspace",
                timeout=15,
            )

            self._exec(
                [
                    "git",
                    "-C",
                    "/work/project",
                    "init",
                    "-q",
                ],
                "Initialize build history",
                timeout=15,
            )

            self._exec(
                [
                    "git",
                    "-C",
                    "/work/project",
                    "config",
                    "user.email",
                    "softwareforge@local",
                ],
                "Configure build history",
                timeout=10,
            )

            self._exec(
                [
                    "git",
                    "-C",
                    "/work/project",
                    "config",
                    "user.name",
                    "SoftwareForge",
                ],
                "Configure build history",
                timeout=10,
            )

            self._exec(
                [
                    "git",
                    "-C",
                    "/work/project",
                    "config",
                    "core.hooksPath",
                    "/dev/null",
                ],
                "Disable Git hooks",
                timeout=10,
            )

            # Verify the builder really is the new Python >=3.11 image before
            # using Spec Kit.  A stale cached image used to surface a confusing
            # "Python is outdated" error only after the user sent a Forge prompt.
            code, out, err = self._exec(
                [
                    "python",
                    "-c",
                    "import sys; print(sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3,11) else 3)",
                ],
                "Verify Build Python",
                timeout=15,
                allow_failure=True,
                workdir="/work/project",
            )
            if code != 0:
                raise BuilderError(
                    "The Build sandbox image is stale and uses an unsupported Python. "
                    "Rebuild it with: docker build --no-cache -f Dockerfile.builder "
                    "-t softwareforge-builder:latest ."
                )

            # The trusted builder image contains a scaffold generated by the
            # official, pinned GitHub Spec Kit CLI. Copy it into the disposable
            # runtime workspace so Build can stay network_mode=none.
            code, out, err = self._exec(
                ["specify", "version"],
                "Verify Spec Kit",
                timeout=20,
                allow_failure=True,
                workdir="/work/project",
            )
            if code != 0:
                raise BuilderError(
                    "Spec Kit is missing from the Build sandbox. Rebuild the image with: "
                    "docker build --no-cache -f Dockerfile.builder "
                    "-t softwareforge-builder:latest ."
                )
            self._exec(
                [
                    "python",
                    "-c",
                    (
                        "from pathlib import Path;import shutil;"
                        "src=Path('/opt/softwareforge-speckit-template');"
                        "dst=Path('/work/project');"
                        "[(shutil.copytree(src/name,dst/name,dirs_exist_ok=True)) "
                        "for name in ('.specify','.softwareforge') if (src/name).exists()]"
                    ),
                ],
                "Install pinned Spec Kit scaffold",
                timeout=30,
                workdir="/work/project",
            )

            self.write_file(
                ".gitignore",
                ".pytest_cache/\n__pycache__/\nnode_modules/\ndist/\nbuild/\n.env\n",
            )
            self.checkpoint("SoftwareForge build workspace")
        except Exception:
            self.end_session()
            raise

    def _exec(self, argv, note="command", timeout=180, allow_failure=False, workdir=None):
        if not self.session_active():
            raise BuilderError("Build sandbox is not active.")

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.session.exec_run, argv, demux=True, workdir=workdir)

        try:
            result = future.result(timeout=timeout)
        except FutureTimeout as exc:
            try:
                self.session.kill()
            except Exception:
                pass
            raise BuilderError(
                "%s exceeded %d seconds. The build sandbox was terminated."
                % (note, timeout)
            ) from exc
        finally:
            pool.shutdown(wait=False)

        stdout, stderr = result.output or (b"", b"")
        out = (stdout or b"").decode("utf-8", "replace")
        err = (stderr or b"").decode("utf-8", "replace")

        if result.exit_code != 0 and not allow_failure:
            raise BuilderError("%s failed:\n%s" % (note, (err or out)[-5000:]))

        return result.exit_code, out, err

    def spec_kit_version(self):
        code, out, err = self._exec(
            ["specify", "version"],
            "Read Spec Kit version",
            timeout=15,
            allow_failure=True,
            workdir="/work/project",
        )
        return (out or err).strip() if code == 0 else "unavailable"

    def spec_kit_guidance(self, stage):
        """Read the real Spec Kit prompt installed by `specify init`.

        The generic integration writes command markdown into the configured
        `.softwareforge/spec-kit` directory.  We search by stage name rather than
        hard-coding one filename so minor upstream filename changes remain safe.
        """
        stage = re.sub(r"[^a-z0-9_-]+", "", str(stage or "").lower())
        if not stage:
            return ""

        script = (
            "from pathlib import Path;import sys;"
            "root=Path('/work/project');stage=sys.argv[1].lower();"
            "bases=[root/'.softwareforge'/'spec-kit',root/'.specify'];"
            "rows=[];"
            "[rows.append(p) for b in bases if b.exists() for p in b.rglob('*') "
            " if p.is_file() and stage in p.name.lower() and p.suffix.lower() in {'.md','.txt'}];"
            "p=sorted(rows,key=lambda x:(0 if 'speckit' in x.name.lower() else 1,len(str(x))))[0] if rows else None;"
            "print(p.read_text(encoding='utf-8',errors='replace')[:18000] if p else '')"
        )
        code, out, err = self._exec(
            ["python", "-c", script, stage],
            "Read Spec Kit %s guidance" % stage,
            timeout=20,
            allow_failure=True,
            workdir="/work/project",
        )
        return out.strip() if code == 0 else ""

    def write_file(self, relative_path, content):
        path = _safe_relative(relative_path)
        data = str(content).encode("utf-8")

        if len(data) > MAX_WRITE_BYTES:
            raise BuilderError("Generated file is too large.")

        parent = str(PurePosixPath(path).parent)
        if parent == ".":
            parent = ""

        target_dir = "/work/project" + (("/" + parent) if parent else "")

        self._exec(
            [
                "python",
                "-c",
                "from pathlib import Path;Path(%r).mkdir(parents=True,exist_ok=True)" % target_dir,
            ],
            "Create project directory",
            timeout=15,
        )

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name=PurePosixPath(path).name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))

        ok = self.session.put_archive(target_dir, buffer.getvalue())
        if not ok:
            raise BuilderError("Could not write generated project file.")

    def read_source(self, relative_path):
        path = _safe_relative(relative_path)
        script = (
            "from pathlib import Path;import sys;"
            "root=Path('/work/project').resolve();"
            "p=(root/sys.argv[1]).resolve();"
            "assert p==root or root in p.parents;"
            "assert p.is_file();"
            "data=p.read_bytes();"
            "assert len(data)<=%d;"
            "sys.stdout.buffer.write(data)"
        ) % MAX_SOURCE_BYTES

        code, out, err = self._exec(
            ["python", "-c", script, path],
            "Read build source",
            timeout=12,
            allow_failure=True,
        )

        if code != 0:
            raise BuilderError("Source file is unavailable or too large.")

        if "\x00" in out[:8000]:
            raise BuilderError("Binary source cannot be previewed.")

        return out

    def list_files(self):
        script = (
            "from pathlib import Path;"
            "root=Path('/work/project');"
            "ignored={'.git','node_modules','dist','build','__pycache__','.pytest_cache'};"
            "rows=[];"
            "[(rows.append(str(p.relative_to(root))) if p.is_file() and not any(x in ignored for x in p.parts) else None) for p in root.rglob('*')];"
            "print('\\n'.join(sorted(rows)[:%d]))"
        ) % MAX_FILES

        _, out, _ = self._exec(
            ["python", "-c", script],
            "List build files",
            timeout=20,
        )

        return [line.strip() for line in out.splitlines() if line.strip()]

    def tree_text(self, max_files=180):
        rows = self.list_files()[:max_files]
        return "\n".join(rows)

    def checkpoint(self, message):
        self._exec(
            [
                "git",
                "-C",
                "/work/project",
                "-c",
                "core.hooksPath=/dev/null",
                "add",
                "-A",
            ],
            "Stage build checkpoint",
            timeout=20,
        )

        code, out, err = self._exec(
            [
                "git",
                "-C",
                "/work/project",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-q",
                "-m",
                str(message)[:140],
            ],
            "Create build checkpoint",
            timeout=25,
            allow_failure=True,
        )

        return code == 0

    def git_diff(self):
        _, out, _ = self._exec(
            ["git", "-C", "/work/project", "diff", "--", "."],
            "Read build diff",
            timeout=15,
        )
        return out[-18000:]

    def run_tests(self):
        """Run only tests/checks that are meaningful in the offline sandbox.

        Build containers intentionally have no network.  The old implementation
        blindly ran `npm test` whenever package.json existed, which made otherwise
        valid static websites look broken simply because node_modules had never
        been downloaded.  Prefer dependency-free syntax checks and only run package
        test commands when their dependencies are already present.
        """
        files = set(self.list_files())
        attempts = []

        py_files = sorted(path for path in files if path.endswith(".py"))
        has_py_tests = any(
            path.startswith("tests/") or PurePosixPath(path).name.startswith("test_")
            for path in py_files
        )

        if py_files:
            if has_py_tests:
                attempts.append((["python", "-m", "pytest", "-q"], "pytest", 120))
            else:
                # Standard-library compile check needs no third-party packages.
                attempts.append((
                    ["python", "-m", "compileall", "-q", "/work/project"],
                    "python compile",
                    90,
                ))

        if "package.json" in files:
            # Only run npm test when node_modules already exists in the project.
            # Generated static sites can still be verified without a package install.
            code, out, err = self._exec(
                ["python", "-c", "from pathlib import Path; print(int(Path('/work/project/node_modules').is_dir()))"],
                "Check frontend dependencies",
                timeout=10,
                allow_failure=True,
            )
            dependencies_ready = out.strip().endswith("1")
            if dependencies_ready:
                attempts.append((["npm", "test", "--", "--runInBand"], "npm test", 120))

        js_files = sorted(path for path in files if path.endswith(".js") and not path.endswith(".min.js"))
        for path in js_files[:20]:
            attempts.append((["node", "--check", "/work/project/" + path], "node --check %s" % path, 30))

        if not attempts:
            html_count = sum(1 for path in files if path.endswith((".html", ".css", ".js")))
            return {
                "ok": True,
                "command": "static inspection",
                "output": (
                    "No dependency-requiring test command was run. "
                    "The offline builder found %d web/source files and preserved the project for Canvas inspection."
                    % html_count
                ),
            }

        logs = []
        overall = True
        labels = []
        for argv, label, timeout in attempts:
            code, out, err = self._exec(
                argv,
                "Run %s" % label,
                timeout=timeout,
                allow_failure=True,
            )
            labels.append(label)
            logs.append("$ %s\n%s" % (label, (out + err)[-7000:]))
            if code != 0:
                overall = False

        return {
            "ok": overall,
            "command": ", ".join(labels),
            "output": "\n\n".join(logs)[-12000:],
        }

    def analyze_graph(self):
        # Keep analysis static even though the build sandbox may execute tests separately.
        self._exec(
            ["rm", "-rf", "/work/project/graphify-out"],
            "Reset build graph",
            timeout=15,
            allow_failure=True,
        )

        self._exec(
            [
                "graphify",
                "extract",
                "/work/project",
                "--code-only",
                "--force",
            ],
            "Refresh build Canvas",
            timeout=240,
        )

        _, out, _ = self._exec(
            [
                "python",
                "-c",
                (
                    "from pathlib import Path;"
                    "rows=[str(p) for p in Path('/work/project').rglob('graph.json') if p.is_file()];"
                    "print(rows[0] if rows else '')"
                ),
            ],
            "Locate build graph",
            timeout=15,
        )

        graph_path = out.strip().splitlines()[-1] if out.strip() else ""
        if not graph_path:
            return {"nodes": [], "edges": []}

        _, raw, _ = self._exec(
            [
                "python",
                "-c",
                "import sys;print(open(sys.argv[1],encoding='utf-8').read())",
                graph_path,
            ],
            "Read build graph",
            timeout=20,
        )

        try:
            return normalize_graph(json.loads(raw))
        except Exception as exc:
            raise BuilderError("Build graph output was invalid.") from exc

    def end_session(self):
        container = self.session
        self.session = None
        self.project_name = ""

        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
