#!/usr/bin/env python3
"""Combine the hatchery check results into one report.

Reads, from the directory given as the first argument:

    checked.json            which values files were checked (check_config.py)
    findings/static.json    static findings (check_config.py)
    findings/loader.json    hatchery loader findings (loader/)
    smoke/*.json            one result per booted container (smoke_test.py)

and writes a Markdown report to the second argument (used as the sticky PR
comment) and to the job summary. With ADVISORY=true (the default) the exit
status is always 0; with ADVISORY=false it is 1 when there are errors.

    report.py build/ report.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MARKER = "<!-- hatchery-config-validation -->"
MAX_LEN = 60000  # GitHub comments are capped at 65536 characters


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def link(file: str, line: int) -> str:
    repo, sha = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("HEAD_SHA") or os.environ.get("GITHUB_SHA")
    label = f"{file}:{line}" if line else file
    if repo and sha:
        return f"[`{label}`](https://github.com/{repo}/blob/{sha}/{file}" + (f"#L{line})" if line else ")")
    return f"`{label}`"


def bullet(f: dict) -> str:
    icon = "❌" if f["level"] == "error" else "⚠️"
    who = f" · {f['container']}" if f.get("container") else ""
    text = f"- {icon} **{f['check']}**{who} ({link(f['file'], f.get('line', 0))})<br>{f['message']}"
    if f.get("hint"):
        text += f"<br>**Fix:** {f['hint']}"
    return text


def main() -> int:
    build, out = Path(sys.argv[1]), Path(sys.argv[2])
    advisory = os.environ.get("ADVISORY", "true") != "false"
    checked = load(build / "checked.json", {"files": [], "containers": 0})
    findings = load(build / "findings" / "static.json", []) + load(build / "findings" / "loader.json", [])
    smoke = [load(p, None) for p in sorted((build / "smoke").glob("*.json"))]
    smoke = [s for s in smoke if s]
    for s in smoke:
        findings += s["findings"]

    # The same container can be booted for several commons; keep one copy.
    seen, unique = set(), []
    for f in findings:
        key = (f["level"], f["file"], f.get("container"), f["message"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    errors = [f for f in unique if f["level"] == "error"]
    warnings = [f for f in unique if f["level"] == "warning"]

    files = checked.get("files", [])
    lines = [MARKER, "## Hatchery config check" + (" (advisory)" if advisory else ""), ""]
    if not files:
        lines += ["No hatchery config changed in this PR, so there was nothing to check."]
    else:
        lines += [
            f"Checked the hatchery config in {len(files)} values file(s)"
            + (f" and booted {len(smoke)} workspace container(s) in Docker." if smoke else "."),
            "" if not advisory else "This check is advisory and does not block merging, but ❌ items are likely to break workspaces.",
            "",
        ]
        if not errors and not warnings:
            lines += ["✅ **No problems found.**", ""]
        else:
            lines += [f"**{len(errors)} problem(s), {len(warnings)} warning(s).** "
                      "Each item links to the line in the values file; the same notes appear inline on the *Files changed* tab.", ""]

        if errors:
            lines += ["### ❌ Problems", ""] + [bullet(f) for f in errors] + [""]

        if smoke:
            lines += ["### Jupyter smoke test", "",
                      "| Workspace | Image | Result | Details |", "|---|---|---|---|"]
            icons = {"passed": "✅ passed", "warnings": "⚠️ warnings", "failed": "❌ problems", "skipped": "⏭️ skipped"}
            for s in smoke:
                details = "; ".join(s.get("notes", [])).replace("|", "\\|")
                lines.append(f"| {s['name']} | `{s['image'].rsplit('/', 1)[-1]}` | {icons.get(s['status'], s['status'])} | {details} |")
            lines.append("")

        if warnings:
            lines += [f"<details><summary>⚠️ {len(warnings)} warning(s)</summary>", ""]
            lines += [bullet(f) for f in warnings] + ["", "</details>", ""]

        lines += ["<sub>What this checks and how to run it locally: "
                  "<code>.github/hatchery/README.md</code>."]
        run = os.environ.get("RUN_URL")
        if run:
            lines[-1] += f" Full logs: [workflow run]({run})."
        lines[-1] += "</sub>"

    text = "\n".join(lines) + "\n"
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "\n\n…report truncated; see the workflow run's job summaries for the rest.\n"
    out.write_text(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text.replace(MARKER, ""))
    print(text)

    if errors and not advisory:
        print(f"{len(errors)} problem(s) found and ADVISORY=false; failing.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
