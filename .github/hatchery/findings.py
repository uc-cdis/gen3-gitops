"""Shared reporting for the hatchery config checks.

Every check records a Finding: what is wrong, where (values file, line,
workspace container), why it matters and how to fix it. Findings are

* printed as GitHub annotations, which show on the PR's "Files changed" tab
  on the offending line;
* written as JSON (``--findings``) so report.py can combine every job into
  one PR comment.

The checks are advisory: report.py decides whether anything fails, based on
the ADVISORY environment variable set in the workflow.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass
class Finding:
    level: str            # "error" | "warning"
    check: str            # short category, e.g. "Jupyter flag"
    file: str             # repo-relative values file
    message: str          # what is wrong
    hint: str = ""        # how to fix it
    container: str = ""   # workspace name, if the finding is about one
    line: int = 0         # 1-based line in `file`, 0 if unknown


def find_line(path: str, *needles: str) -> int:
    """Line of the first needle found in the file (tried in order)."""
    try:
        lines = (REPO / path).read_text().splitlines()
    except OSError:
        return 0
    for needle in needles:
        if not needle:
            continue
        for i, text in enumerate(lines, 1):
            if needle in text:
                return i
    return 0


def container_line(path: str, container: str, *needles: str) -> int:
    """Line of a needle inside the given container's block, else the
    container's "name" line, else the first match anywhere."""
    try:
        lines = (REPO / path).read_text().splitlines()
    except OSError:
        return 0
    name_rx = re.compile(r'["\']?name["\']?\s*:\s*["\']?' + re.escape(container) + r'["\']?\s*,?\s*$')
    start = next((i for i, t in enumerate(lines) if container and name_rx.search(t.strip())), None)
    if start is not None:
        # Containers are a few dozen lines; look around the name for the field.
        lo, hi = max(0, start - 40), min(len(lines), start + 80)
        for needle in needles:
            if not needle:
                continue
            # Prefer the nearest match to the name line.
            hits = [i for i in range(lo, hi) if needle in lines[i]]
            if hits:
                return min(hits, key=lambda i: abs(i - start)) + 1
        return start + 1
    return find_line(path, *needles, "hatchery:")


@dataclass
class Findings:
    items: list[Finding] = field(default_factory=list)
    prefix: str = "Hatchery"

    @property
    def errors(self) -> int:
        return sum(f.level == "error" for f in self.items)

    @property
    def warnings(self) -> int:
        return sum(f.level == "warning" for f in self.items)

    def add(self, level: str, check: str, file: str, message: str, *, hint: str = "",
            container: str = "", needle: str | tuple[str, ...] = (), line: int = 0) -> Finding:
        needles = (needle,) if isinstance(needle, str) else tuple(needle)
        if not line:
            line = container_line(file, container, *needles) if container else find_line(file, *needles, "hatchery:")
        f = Finding(level, check, file, message, hint, container, line)
        self.items.append(f)
        self._annotate(f)
        return f

    def error(self, check: str, file: str, message: str, **kw) -> Finding:
        return self.add("error", check, file, message, **kw)

    def warning(self, check: str, file: str, message: str, **kw) -> Finding:
        return self.add("warning", check, file, message, **kw)

    def _annotate(self, f: Finding) -> None:
        where = f"file={f.file}" + (f",line={f.line}" if f.line else "")
        title = f"{self.prefix}: {f.check}" + (f" ({f.container})" if f.container else "")
        body = f.message + (f" — Fix: {f.hint}" if f.hint else "")
        # Annotation properties and messages must be single-line and escaped.
        body = body.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        title = title.replace("%", "%25").replace(",", "%2C").replace(":", "%3A").replace("\n", " ")
        print(f"::{f.level} {where},title={title}::{body}", flush=True)

    def extend_from(self, path: Path) -> None:
        if path.exists():
            self.items.extend(Finding(**d) for d in json.loads(path.read_text()))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(f) for f in self.items], indent=2))
