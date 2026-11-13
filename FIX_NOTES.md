# SoftwareForge UI + Build hotfix

This patch targets the two failures visible in the latest screenshots.

## Chat / Forge UI

- One-line user questions no longer use a Tk `Text` widget, which was the cause of the giant empty message cards on Tk/X11. User messages now size naturally using a wrapped label.
- Markdown answer height no longer depends on `Text.count(..., displaylines)` before layout; that Tk behavior could massively over-count wrapped lines. Answers use the renderer's deterministic content estimate instead.
- Analyze Agent and Forge composers now use explicit dark surfaces and bright text instead of transparent textbox backgrounds.
- Main chat font is larger and uses DejaVu Sans in the Linux/VNC devcontainer for consistent rendering.
- Composer font is 15pt-equivalent and both composers auto-grow only up to five lines.
- Forge now has an actual visible placeholder and restores normal text color on focus/prefill.
- Analyze Agent retains Searching -> Reading evidence -> Thinking -> Generating states. Forge retains Thinking/Planning -> Generating states.

## Build / Spec Kit

- The desktop `requirements.txt` no longer attempts to install Spec Kit on Python 3.9. Spec Kit lives only in the builder image.
- Builder base is Python 3.12, satisfying Spec Kit's Python >=3.11 requirement while keeping broader package compatibility.
- Removed the invalid `specify init --non-interactive` flag. The pinned generic integration is explicit, so current Spec Kit supports non-interactive image initialization without that flag.
- Builder performs a clear Python and Spec Kit preflight and tells the user to rebuild the image with `--no-cache` if an old cached image is detected.
- Forge specification chat no longer fails just because a stale/missing builder image prevents reading optional Spec Kit guidance; it can still produce spec/plan/tasks and surfaces the builder problem when implementation actually starts.
- Website generation is biased toward dependency-free HTML/CSS/JavaScript unless the approved plan explicitly requests a framework, because the runtime Build sandbox is intentionally offline.
- Verification no longer blindly runs `npm test` just because `package.json` exists. It only does dependency-requiring npm tests when node_modules is already present, and otherwise uses dependency-free syntax/static checks.

## Required rebuild

```bash
docker build --no-cache -f Dockerfile.builder -t softwareforge-builder:latest .
```

The old cached builder image is not compatible with this patch.
