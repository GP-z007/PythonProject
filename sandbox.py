from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import tarfile
import time

import docker
from docker.errors import DockerException

from analyzer import normalize_graph


IMAGE = "softwareforge-analyzer:latest"

GITHUB_RE = re.compile(
    r"^https://github\.com/"
    r"[A-Za-z0-9_.-]+/"
    r"[A-Za-z0-9_.-]+"
    r"(?:\.git)?/?$"
)

MAX_LOCAL_FILES = 50000
MAX_LOCAL_BYTES = 600 * 1024 * 1024
MAX_GRAPH_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 400 * 1024


class SandboxError(RuntimeError):
    pass


class SandboxManager:
    def __init__(self):
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as exc:
            raise SandboxError(
                "Docker is not available."
            ) from exc

        self.session = None
        self.session_source = ""
        self.session_kind = ""
        self.session_commit = ""

    def image_ready(self):
        try:
            self.client.images.get(
                IMAGE
            )
            return True
        except Exception:
            return False

    def session_active(self):
        if self.session is None:
            return False

        try:
            self.session.reload()
            return (
                self.session.status
                == "running"
            )
        except Exception:
            return False

    def _new_container(
        self,
        network,
    ):
        return self.client.containers.run(
            IMAGE,
            command=[
                "sleep",
                "infinity",
            ],
            detach=True,
            auto_remove=False,
            network_mode=(
                "bridge"
                if network
                else "none"
            ),
            read_only=True,
            user="10001:10001",
            privileged=False,
            cap_drop=[
                "ALL"
            ],
            security_opt=[
                "no-new-privileges:true",
            ],
            pids_limit=192,
            mem_limit="1g",
            memswap_limit="1g",
            nano_cpus=2000000000,
            init=True,
            tmpfs={
                "/work": (
                    "rw,noexec,nosuid,nodev,"
                    "size=768m,uid=10001,gid=10001"
                ),
                "/tmp": (
                    "rw,noexec,nosuid,nodev,"
                    "size=64m,uid=10001,gid=10001"
                ),
            },
            environment={
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONNOUSERSITE": "1",
            },
        )

    def _exec(
        self,
        container,
        argv,
        note="command",
        timeout=240,
    ):
        pool = ThreadPoolExecutor(
            max_workers=1
        )

        future = pool.submit(
            container.exec_run,
            argv,
            demux=True,
        )

        try:
            result = future.result(
                timeout=timeout
            )
        except FutureTimeout as exc:
            try:
                container.kill()
            except Exception:
                pass

            raise SandboxError(
                "%s exceeded %d seconds. "
                "The sandbox was terminated."
                % (
                    note,
                    timeout,
                )
            ) from exc
        finally:
            pool.shutdown(
                wait=False
            )

        stdout, stderr = (
            result.output
            or (
                b"",
                b"",
            )
        )

        out = (
            stdout
            or b""
        ).decode(
            "utf-8",
            "replace",
        )

        err = (
            stderr
            or b""
        ).decode(
            "utf-8",
            "replace",
        )

        if result.exit_code != 0:
            raise SandboxError(
                "%s failed:\n%s"
                % (
                    note,
                    (err or out)[-4500:],
                )
            )

        return out

    def _disconnect_all_networks(
        self,
        container,
    ):
        container.reload()

        networks = list(
            (
                container.attrs
                .get(
                    "NetworkSettings",
                    {},
                )
                .get(
                    "Networks",
                    {},
                )
                or {}
            ).keys()
        )

        for name in networks:
            try:
                self.client.networks.get(
                    name
                ).disconnect(
                    container,
                    force=True,
                )
            except Exception:
                pass

        container.reload()

        remaining = (
            container.attrs
            .get(
                "NetworkSettings",
                {},
            )
            .get(
                "Networks",
                {},
            )
            or {}
        )

        if remaining:
            raise SandboxError(
                "Could not isolate analyzer networking."
            )

    def _read_file(
        self,
        container,
        path,
        max_bytes=MAX_GRAPH_BYTES,
    ):
        if (
            not path.startswith(
                "/work/"
            )
            or ".." in Path(path).parts
        ):
            raise SandboxError(
                "Refusing to read output outside /work."
            )

        size_text = self._exec(
            container,
            [
                "python",
                "-c",
                (
                    "import os,sys;"
                    "p=sys.argv[1];"
                    "print(os.path.getsize(p) "
                    "if os.path.isfile(p) else -1)"
                ),
                path,
            ],
            "Output size check",
            timeout=15,
        ).strip()

        try:
            size = int(
                size_text
            )
        except ValueError as exc:
            raise SandboxError(
                "Could not determine analyzer output size."
            ) from exc

        if size < 0:
            raise SandboxError(
                "Analyzer output was not found."
            )

        if size > max_bytes:
            raise SandboxError(
                "Analyzer output exceeded safety limit."
            )

        result = container.exec_run(
            [
                "python",
                "-c",
                (
                    "import sys;"
                    "sys.stdout.buffer.write("
                    "open(sys.argv[1],'rb').read())"
                ),
                path,
            ],
            demux=True,
        )

        stdout, stderr = (
            result.output
            or (
                b"",
                b"",
            )
        )

        if result.exit_code != 0:
            raise SandboxError(
                "Could not read analyzer output."
            )

        data = stdout or b""

        if len(data) != size:
            raise SandboxError(
                "Analyzer output changed while being read."
            )

        return data

    def _extract_graph(
        self,
        container,
    ):
        container.exec_run([
            "rm",
            "-rf",
            "/work/repo/graphify-out",
        ])

        log = self._exec(
            container,
            [
                "graphify",
                "extract",
                "/work/repo",
                "--code-only",
                "--force",
            ],
            "Static analysis",
            timeout=300,
        )

        locate = self._exec(
            container,
            [
                "python",
                "-c",
                (
                    "from pathlib import Path;"
                    "print('\\n'.join("
                    "str(p) for p in "
                    "Path('/work').rglob('graph.json') "
                    "if p.is_file()))"
                ),
            ],
            "Find graph output",
            timeout=20,
        )

        candidates = [
            line.strip()
            for line in locate.splitlines()
            if line.strip().startswith(
                "/work/"
            )
        ]

        preferred = (
            "/work/repo/graphify-out/graph.json",
            "/work/graphify-out/graph.json",
        )

        graph_path = next(
            (
                path
                for path in preferred
                if path in candidates
            ),
            None,
        )

        if graph_path is None and candidates:
            graph_path = candidates[0]

        if graph_path is None:
            raise SandboxError(
                "Static analysis completed but no graph "
                "was produced.\n\n"
                + log[-2500:]
            )

        raw_bytes = self._read_file(
            container,
            graph_path,
        )

        try:
            raw = json.loads(
                raw_bytes
            )
        except json.JSONDecodeError as exc:
            raise SandboxError(
                "Static analyzer produced invalid JSON."
            ) from exc

        return normalize_graph(
            raw
        )

    def _clone(
        self,
        container,
        url,
    ):
        url = url.strip()

        if not GITHUB_RE.fullmatch(
            url
        ):
            raise SandboxError(
                "Use a normal public "
                "https://github.com/owner/repository URL."
            )

        self._exec(
            container,
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "clone",
                "--no-recurse-submodules",
                "--",
                url,
                "/work/repo",
            ],
            "Repository clone",
            timeout=180,
        )

        self._disconnect_all_networks(
            container
        )

        self._exec(
            container,
            [
                "git",
                "-C",
                "/work/repo",
                "config",
                "--local",
                "core.hooksPath",
                "/dev/null",
            ],
            "Git hardening",
            timeout=15,
        )

    def _safe_local_tar(
        self,
        folder,
    ):
        folder = Path(
            folder
        ).resolve()

        if not folder.is_dir():
            raise SandboxError(
                "Local repository is not a folder."
            )

        buffer = io.BytesIO()
        count = 0
        total = 0

        with tarfile.open(
            fileobj=buffer,
            mode="w",
        ) as archive:
            for base, dirs, names in os.walk(
                folder,
                followlinks=False,
            ):
                dirs[:] = [
                    directory
                    for directory in dirs
                    if directory != ".git"
                ]

                base_path = Path(
                    base
                )

                for name in names:
                    path = (
                        base_path
                        / name
                    )

                    try:
                        info = path.lstat()
                    except OSError:
                        continue

                    if not stat.S_ISREG(
                        info.st_mode
                    ):
                        continue

                    count += 1
                    total += info.st_size

                    if count > MAX_LOCAL_FILES:
                        raise SandboxError(
                            "Local project contains too many files."
                        )

                    if total > MAX_LOCAL_BYTES:
                        raise SandboxError(
                            "Local project is too large "
                            "for the bounded sandbox."
                        )

                    relative = (
                        path.relative_to(
                            folder
                        )
                    )

                    archive.add(
                        path,
                        arcname=str(
                            Path("repo")
                            / relative
                        ),
                        recursive=False,
                    )

        return buffer.getvalue()

    def _stream_tar_into_work(
        self,
        container,
        tar_bytes,
    ):
        created = self.client.api.exec_create(
            container.id,
            [
                "tar",
                "-xf",
                "-",
                "-C",
                "/work",
                "--no-same-owner",
                "--no-same-permissions",
            ],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
        )

        exec_id = created[
            "Id"
        ]

        wrapper = self.client.api.exec_start(
            exec_id,
            detach=False,
            tty=False,
            socket=True,
        )

        raw_socket = getattr(
            wrapper,
            "_sock",
            wrapper,
        )

        try:
            raw_socket.sendall(
                tar_bytes
            )

            try:
                raw_socket.shutdown(
                    socket.SHUT_WR
                )
            except Exception:
                pass

            while True:
                try:
                    chunk = raw_socket.recv(
                        65536
                    )
                except Exception:
                    break

                if not chunk:
                    break
        finally:
            try:
                wrapper.close()
            except Exception:
                pass

        deadline = (
            time.time()
            + 30
        )

        while time.time() < deadline:
            info = self.client.api.exec_inspect(
                exec_id
            )

            if not info.get(
                "Running"
            ):
                if info.get(
                    "ExitCode"
                ) != 0:
                    raise SandboxError(
                        "Could not copy local project "
                        "into isolated tmpfs."
                    )

                return

            time.sleep(
                0.05
            )

        raise SandboxError(
            "Local project copy timed out."
        )

    def start_github(
        self,
        url,
    ):
        if self.session_active():
            raise SandboxError(
                "End the current analysis before "
                "opening another repository."
            )

        container = self._new_container(
            network=True
        )

        self.session = container

        try:
            self._clone(
                container,
                url,
            )

            commit = self._exec(
                container,
                [
                    "git",
                    "-C",
                    "/work/repo",
                    "rev-parse",
                    "HEAD",
                ],
                "Read commit",
                timeout=15,
            ).strip()

            graph = self._extract_graph(
                container
            )

            self.session_source = (
                url.strip()
            )
            self.session_kind = "github"
            self.session_commit = commit

            return {
                "graph": graph,
                "commit": commit,
                "source": self.session_source,
                "kind": self.session_kind,
            }

        except Exception:
            self.end_session()
            raise

    def start_local(
        self,
        folder,
    ):
        if self.session_active():
            raise SandboxError(
                "End the current analysis before "
                "opening another repository."
            )

        container = self._new_container(
            network=False
        )

        self.session = container

        try:
            payload = self._safe_local_tar(
                folder
            )

            self._stream_tar_into_work(
                container,
                payload,
            )

            graph = self._extract_graph(
                container
            )

            self.session_source = str(
                Path(
                    folder
                ).resolve()
            )
            self.session_kind = "local"
            self.session_commit = ""

            return {
                "graph": graph,
                "commit": "",
                "source": self.session_source,
                "kind": self.session_kind,
            }

        except Exception:
            self.end_session()
            raise

    def read_source(
        self,
        relative_path,
    ):
        if not self.session_active():
            raise SandboxError(
                "Analysis session is no longer active."
            )

        relative_path = str(
            relative_path
            or ""
        ).replace(
            "\\",
            "/",
        )

        pure = PurePosixPath(
            relative_path
        )

        if (
            not relative_path
            or pure.is_absolute()
            or ".." in pure.parts
            or len(
                relative_path
            ) > 420
        ):
            raise SandboxError(
                "Unsafe source path."
            )

        script = (
            "from pathlib import Path;"
            "import sys;"
            "root=Path('/work/repo').resolve();"
            "candidate=(root / sys.argv[1]).resolve();"
            "ok=(candidate == root or root in candidate.parents);"
            "size=(candidate.stat().st_size if ok and candidate.is_file() else -1);"
            "print(size);"
        )

        size_text = self._exec(
            self.session,
            [
                "python",
                "-c",
                script,
                relative_path,
            ],
            "Source size check",
            timeout=10,
        ).strip()

        try:
            size = int(
                size_text
            )
        except ValueError as exc:
            raise SandboxError(
                "Could not inspect source file."
            ) from exc

        if size < 0:
            raise SandboxError(
                "Source file is unavailable."
            )

        if size > MAX_SOURCE_BYTES:
            raise SandboxError(
                "Source file is too large to preview."
            )

        read_script = (
            "from pathlib import Path;"
            "import sys;"
            "root=Path('/work/repo').resolve();"
            "candidate=(root / sys.argv[1]).resolve();"
            "assert candidate == root or root in candidate.parents;"
            "data=candidate.read_bytes();"
            "sys.stdout.buffer.write(data)"
        )

        result = self.session.exec_run(
            [
                "python",
                "-c",
                read_script,
                relative_path,
            ],
            demux=True,
        )

        stdout, stderr = (
            result.output
            or (
                b"",
                b"",
            )
        )

        if result.exit_code != 0:
            raise SandboxError(
                "Could not preview source file."
            )

        data = stdout or b""

        if b"\x00" in data[:8192]:
            raise SandboxError(
                "Binary files are not shown in the code preview."
            )

        return data.decode(
            "utf-8",
            "replace",
        )

    def end_session(
        self,
    ):
        container = self.session

        self.session = None
        self.session_source = ""
        self.session_kind = ""
        self.session_commit = ""

        if container is not None:
            try:
                container.remove(
                    force=True
                )
            except Exception:
                pass
