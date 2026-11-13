from sandbox import IMAGE, SandboxManager
from builder import BUILDER_IMAGE, BuilderSandbox


def assert_common_hardening(container, expected_pids):
    container.reload()
    host = container.attrs["HostConfig"]

    assert container.attrs["Config"]["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert "ALL" in (host.get("CapDrop") or [])
    assert host["NetworkMode"] == "none"
    assert host["PidsLimit"] == expected_pids
    assert any(
        item == "no-new-privileges:true"
        for item in (host.get("SecurityOpt") or [])
    )


# Analyze sandbox ------------------------------------------------------
analyzer = SandboxManager()
assert analyzer.image_ready(), "Build %s first." % IMAGE

container = analyzer._new_container(network=False)
try:
    assert_common_hardening(container, 192)

    result = container.exec_run([
        "python",
        "-c",
        (
            "import os,pathlib;"
            "assert os.getuid()==10001;"
            "pathlib.Path('/tmp/ok').write_text('ok');"
            "print('analyze uid',os.getuid())"
        ),
    ])
    assert result.exit_code == 0, result.output
finally:
    container.remove(force=True)


# Build sandbox --------------------------------------------------------
builder = BuilderSandbox()
assert builder.image_ready(), "Build %s first." % BUILDER_IMAGE

builder.start("security-smoke")
try:
    assert_common_hardening(builder.session, 256)

    # The runtime is network-disabled, so the real pinned Spec Kit scaffold must
    # already be baked into the trusted builder image.
    assert builder.spec_kit_version() != "unavailable"
    assert builder.spec_kit_guidance("specify"), "Spec Kit specify guidance missing."

    # Build is writable only under the bounded /work tmpfs.
    builder.write_file("hello.py", "print('hello')\n")
    assert "print('hello')" in builder.read_source("hello.py")

    # Network remains disabled by default.
    builder.session.reload()
    assert builder.session.attrs["HostConfig"]["NetworkMode"] == "none"
finally:
    builder.end_session()

print("SECURITY SMOKE TESTS: PASS")
