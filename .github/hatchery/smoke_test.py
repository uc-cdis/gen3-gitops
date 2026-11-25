#!/usr/bin/env python3
"""Boot one hatchery workspace container in Docker the way hatchery would.

Input is one entry of the matrix written by check_config.py. Steps:

1. pull the image (private ECR images are skipped with a warning unless
   REQUIRE_PRIVATE_IMAGES=true, e.g. when CI has no AWS credentials);
2. check the Jupyter flags against the Jupyter inside the image
   (in_image/check_flags.py);
3. run the container with the configured command, args, env, uid/fs-gid and
   volume mount points, run lifecycle-post-start like the kubelet does, and
   poll the ready-probe until it answers 2xx/3xx;
4. scan the Jupyter log for config warnings and fatal errors;
5. validate the notebooks baked into the image and the default_url
   (in_image/check_notebooks.py).

Problems are printed as GitHub annotations on the line of the values file that
defines the container, and a short report goes to the job summary.

    smoke_test.py matrix-entry.json [result.json]

The exit status is 0 even when problems are found; non-zero means the test
itself broke. report.py turns result.json into the PR comment.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from findings import Findings  # noqa: E402

HERE = Path(__file__).resolve().parent
IN_IMAGE = HERE / "in_image"
MOUNT = "/hatchery-ci"  # where in_image/ is mounted inside the container
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT", "600"))
REQUIRE_PRIVATE = os.environ.get("REQUIRE_PRIVATE_IMAGES", "false") == "true"
PLATFORM = os.environ.get("DOCKER_PLATFORM", "linux/amd64")

FLAG_RE = re.compile(r"^--([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
LOG_ERRORS = [
    (re.compile(r"Config option `[^`]+` not recognized by `[^`]+`"), "Jupyter ignored a flag"),
    (re.compile(r"^\[C [^\]]*\].*$", re.M), "Jupyter logged a critical error"),
]
LOG_WARNINGS = [
    (re.compile(r"^\[W [^\]]*\].*(deprecated|has moved from).*$", re.M | re.I), "deprecated option"),
]

# Shell snippet that finds the Python Jupyter runs with (the `jupyter` script's
# shebang), falling back to python3/python. Old images lack `command -v` in sh,
# hence `which` too.
FIND_PY = r'''
J=$(command -v jupyter 2>/dev/null || which jupyter 2>/dev/null)
PY=""
if [ -n "$J" ]; then PY=$(sed -n '1s/^#! *//p' "$J"); fi
case "$PY" in */env\ *|*/sh*|"") PY="";; esac
for c in "$PY" python3 python; do
  [ -n "$c" ] && command -v $c >/dev/null 2>&1 && { exec $c "$@"; }
done
echo '{"error": "no python found in image"}'
'''


class Result:
    """Findings for one container, annotated on the values file that defines it."""

    def __init__(self, entry: dict):
        self.entry = entry
        self.file = entry["sources"][0]
        self.findings = Findings(prefix="Jupyter smoke test")
        self.notes: list[str] = []
        self.status = "passed"

    def _add(self, level: str, check: str, msg: str, hint: str, needle) -> None:
        also = entry_others(self.entry)
        self.findings.add(level, check, self.file, msg + also, hint=hint,
                          container=self.entry["name"], needle=needle)

    def error(self, check: str, msg: str, hint: str = "", needle=()) -> None:
        self._add("error", check, msg, hint, needle)

    def warning(self, check: str, msg: str, hint: str = "", needle=()) -> None:
        self._add("warning", check, msg, hint, needle)

    @property
    def errors(self) -> list:
        return [f for f in self.findings.items if f.level == "error"]

    @property
    def warnings(self) -> list:
        return [f for f in self.findings.items if f.level == "warning"]


def entry_others(entry: dict) -> str:
    others = entry["sources"][1:]
    return f" (same container also in: {', '.join(others)})" if others else ""


def sh(cmd: list[str], check=False, timeout=None, **kw) -> subprocess.CompletedProcess:
    shown = [a if len(a) < 120 else a[:40] + "…" for a in cmd]  # keep the inline sh script short
    print("+", shlex.join(shown), flush=True)
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout, **kw)


def flags(args: list[str]) -> dict[str, str]:
    out = {}
    for a in args:
        m = FLAG_RE.match(a)
        if m:
            v = m.group(3)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[f"{m.group(1)}.{m.group(2)}"] = v
    return out


def pick(fl: dict[str, str], *traits: str) -> str:
    for k, v in fl.items():
        if k.split(".")[1] in traits:
            return v
    return ""


def host_memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 0


def mem_bytes(q: str) -> int:
    m = re.match(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|K|M|G|T)?$", q or "")
    if not m:
        return 0
    mult = {None: 1, "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
            "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}[m.group(2)]
    return int(float(m.group(1)) * mult)


def cpu_cores(q: str) -> float:
    m = re.match(r"^(\d+(?:\.\d+)?)(m?)$", q)
    if not m:
        return 0.0
    return float(m.group(1)) / (1000 if m.group(2) else 1)


def docker_common(c: dict) -> list[str]:
    """Flags shared by the flag check and the real boot: platform, user, env."""
    out = ["--platform", PLATFORM]
    uid, gid, fsgid = c.get("user-uid"), c.get("group-uid"), c.get("fs-gid")
    if uid:
        out += ["--user", f"{uid}:{gid}" if gid else str(uid)]
    if fsgid:
        out += ["--group-add", str(fsgid)]  # what fsGroup does for the process
    env = {"GEN3_ENDPOINT": "ci.example.org", "API_KEY": "", "API_KEY_ID": ""}
    env.update(c.get("env") or {})
    for k, v in env.items():
        out += ["-e", f"{k}={v}"]
    return out


def pull(r: Result) -> bool:
    image = r.entry["image"]
    p = sh(["docker", "pull", "--platform", PLATFORM, image], timeout=1800)
    if p.returncode == 0:
        return True
    tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]
    if r.entry.get("private") and not REQUIRE_PRIVATE:
        r.warning("Skipped", f"not tested: CI cannot pull this private image ({tail[0]}).",
                  "nothing to fix in this PR. To test private images, set the HATCHERY_ECR_ROLE_ARN repository variable (see .github/hatchery/README.md).",
                  needle=image)
        r.notes.append("skipped: private image and no registry credentials")
        r.status = "skipped"
    else:
        r.error("Image", f"the image could not be pulled: {tail[0]}",
                "check the image name and tag exist in the registry (a typo here means users get ErrImagePull).", needle=image)
    return False


def in_image(c: dict, image: str, script: str, args: list[str], container: str | None = None) -> dict | None:
    """Run an in_image/ script with the image's Python, either in a throwaway
    container or (container=...) inside the running workspace."""
    inner = ["sh", "-c", FIND_PY, "sh", f"{MOUNT}/{script}", *args]
    if container:
        cmd = ["docker", "exec", container, *inner]
    else:
        cmd = ["docker", "run", "--rm", *docker_common(c), "-v", f"{IN_IMAGE}:{MOUNT}:ro",
               "--entrypoint", inner[0], image, *inner[1:]]
    p = sh(cmd, timeout=300)
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    print(p.stdout[-2000:], p.stderr[-2000:], sep="\n")
    return None


def check_flags(r: Result) -> None:
    c = r.entry["container"]
    args = [a for a in (c.get("args") or []) if FLAG_RE.match(a)]
    if not args:
        return
    out = in_image(c, r.entry["image"], "check_flags.py", args)
    if out is None or "error" in out:
        r.warning("Jupyter flag", "the flag check could not run inside the image (no Python with traitlets found); flags were only checked by booting.")
        return
    if out.get("versions"):
        r.notes.append("Jupyter: " + ", ".join(f"{k} {v}" for k, v in sorted(out["versions"].items())))
    ok = 0
    for res in out["results"]:
        if res["level"] == "error":
            r.error("Jupyter flag", f'{res["arg"]}: {res["message"]}',
                    flag_hint(res), needle=res["arg"])
        elif res["level"] == "warning":
            r.warning("Jupyter flag", f'{res["arg"]}: {res["message"]}',
                      flag_hint(res), needle=res["arg"])
        else:
            ok += 1
    r.notes.append(f"{ok}/{len(out['results'])} Jupyter flags valid")


def flag_hint(res: dict) -> str:
    m = res["message"]
    if "did you mean" in m:
        return "fix the spelling of the setting name."
    if "no Jupyter class" in m:
        return "Jupyter ignores this flag entirely. Check the class name; on Jupyter Server 2 most settings live on ServerApp."
    if "forwarded to" in m:
        return "it works today, but rename it to the setting named above before the shim is removed."
    if "deprecated" in m:
        return "switch to the replacement named above; deprecated settings are removed in later Jupyter releases."
    if "invalid value" in m:
        return "Jupyter refuses to start with this value; use the type shown above."
    return "remove the flag or rename it to a setting that exists in this image's Jupyter."


def log_hint(line: str) -> str:
    if "not recognized" in line:
        return "Jupyter ignored this setting; check its name against the image's Jupyter version."
    if "has moved from" in line:
        return "rename the flag as the message says; it relies on a deprecated compatibility shim."
    if "deprecated" in line:
        return "switch to the replacement named in the message."
    return "see the container log in the job summary."


def notebook_hint(res: dict) -> str:
    m = res["message"]
    if "default_url" in m:
        return "point default_url at a file that exists in the image, or add the file to the image."
    if "kernel" in m:
        return "install that kernel in the image, or change the notebook's kernelspec."
    return "re-save the notebook from Jupyter, or fix it in the image's source repo."


def boot(r: Result, name: str) -> bool:
    c = r.entry["container"]
    port = c.get("target-port") or 8888
    cmd = ["docker", "run", "-d", "--name", name, *docker_common(c),
           "-p", f"127.0.0.1::{port}", "-v", f"{IN_IMAGE}:{MOUNT}:ro"]
    # Volumes hatchery mounts. Empty and writable, like a fresh workspace.
    mounts = {"/data", "/.gen3"}
    for key in ("user-volume-location", "gen3-volume-location"):
        if c.get(key):
            mounts.add(c[key])
    for m in sorted(mounts):
        cmd += ["--mount", f"type=tmpfs,destination={m},tmpfs-mode=1777"]
    if c.get("use-shared-memory") == "true":
        cmd += ["--shm-size", "1g"]
    # Apply the configured limits when the runner can honour them, so a
    # workspace that cannot boot within its own memory limit fails here.
    mem = mem_bytes(str(c.get("memory-limit", "")))
    if mem and mem < host_memory_bytes() * 0.9:
        cmd += ["--memory", str(mem)]
    cpus = min(cpu_cores(str(c.get("cpu-limit", ""))), os.cpu_count() or 1)
    if cpus > 0:
        cmd += ["--cpus", f"{cpus:g}"]
    command = c.get("command") or []
    if command:
        cmd += ["--entrypoint", command[0], r.entry["image"], *command[1:]]
    else:
        cmd += [r.entry["image"]]
    cmd += c.get("args") or []

    p = sh(cmd)
    if p.returncode != 0:
        r.error("Boot", f"the container could not be started: {p.stderr.strip()[-300:]}",
                "check command, args and user-uid; see the job log for the full docker output.", needle="command")
        return False

    # The kubelet runs postStart right after the container starts; if it fails
    # the container is killed and the workspace never becomes ready.
    hook = c.get("lifecycle-post-start") or []
    if hook:
        h = sh(["docker", "exec", name, *hook], timeout=120)
        if h.returncode != 0:
            r.error("Post-start hook", f"lifecycle-post-start exited {h.returncode}: {(h.stderr or h.stdout).strip()[-300:]}. "
                    "Kubernetes kills a container whose postStart hook fails, so the workspace would never start.",
                    "make the hook tolerant of failures (end it with `; true`) or fix the failing command.", needle="lifecycle-post-start")

    host_port = sh(["docker", "port", name, str(port)]).stdout.strip().splitlines()
    if not host_port:
        r.error("Boot", f"target-port {port} could not be exposed.", "check target-port matches the port Jupyter listens on.", needle="target-port")
        return False
    url = f"http://{host_port[0].replace('0.0.0.0', '127.0.0.1')}{c.get('ready-probe', '/')}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    start, last = time.time(), ""
    while time.time() - start < READY_TIMEOUT:
        state = json.loads(sh(["docker", "inspect", "-f", "{{json .State}}", name]).stdout or "{}")
        if not state.get("Running"):
            r.error("Boot", f"the workspace exited with code {state.get('ExitCode')} before it became ready, so users would see it fail to launch.",
                    "the container log in the job summary usually says why (often a bad flag or a missing start script).", needle="command")
            return False
        try:
            code = opener.open(url, timeout=5).status
        except urllib.error.HTTPError as e:
            code = e.code
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            code, last = None, str(e)
        if code is not None:
            last = f"HTTP {code}"
            if 200 <= code < 400:  # kubelet httpGet success range
                r.notes.append(f"ready-probe {c.get('ready-probe')} answered {code} after {time.time() - start:.0f}s")
                return True
        time.sleep(3)
    r.error("Probe", f"ready-probe {c.get('ready-probe')} never succeeded within {READY_TIMEOUT}s (last response: {last}). Hatchery would show the workspace as launching forever.",
            "the probe path must be served by Jupyter: usually base_url, or base_url + 'lab'. Check target-port too.", needle="ready-probe")
    return False


def scan_logs(r: Result, name: str) -> str:
    p = sh(["docker", "logs", name])
    logs = p.stdout + p.stderr
    seen = set()
    for rx, what in LOG_ERRORS:
        for m in rx.finditer(logs):
            line = m.group(0).strip()
            if line not in seen:
                seen.add(line)
                r.error("Jupyter log", f"{what}: {line}", log_hint(line))
    for rx, what in LOG_WARNINGS:
        for m in rx.finditer(logs):
            line = m.group(0).strip()
            if line not in seen:
                seen.add(line)
                r.warning("Jupyter log", f"{what}: {line}", log_hint(line))
    return logs


def check_notebooks(r: Result, name: str) -> None:
    c = r.entry["container"]
    fl = flags(c.get("args") or [])
    root = pick(fl, "root_dir", "notebook_dir") or ""
    if not root:
        root = sh(["docker", "exec", name, "sh", "-c", "echo $HOME"]).stdout.strip() or "/home/jovyan"
    out = in_image(c, r.entry["image"], "check_notebooks.py", [root, pick(fl, "default_url")], container=name)
    if out is None:
        r.warning("Notebooks", "the notebook check could not run inside the image (no Python with nbformat found).")
        return
    bad = 0
    for res in out["results"]:
        msg = f'{res["path"]}: {res["message"]}' if res["path"] else res["message"]
        if res["level"] == "error":
            bad += 1
            r.error("Notebooks", msg, notebook_hint(res), needle="default_url" if "default_url" in res["message"] else ())
        else:
            r.warning("Notebooks", msg, notebook_hint(res))
    r.notes.append(f"{out['checked']} notebook(s) in image checked, {bad} invalid")


def write_summary(r: Result, logs: str) -> None:
    if r.status != "skipped":
        r.status = "failed" if r.errors else ("warnings" if r.warnings else "passed")
    icon = {"failed": "❌ problems found", "warnings": "⚠️ passed with warnings", "passed": "✅ passed",
            "skipped": "⏭️ skipped"}[r.status]
    lines = [f"### {r.entry['name']}: {icon}", "",
             f"- image: `{r.entry['image']}`",
             f"- defined in: {', '.join(f'`{s}`' for s in r.entry['sources'])}"]
    lines += [f"- {n}" for n in r.notes]
    for title, items in (("Problems", r.errors), ("Warnings", r.warnings)):
        if items:
            lines += ["", f"**{title}**", ""]
            lines += [f"- {f.message}" + (f"<br>Fix: {f.hint}" if f.hint else "") for f in items]
    if logs and r.errors:
        lines += ["", "<details><summary>container log (last 100 lines)</summary>", "", "```",
                  *logs.strip().splitlines()[-100:], "```", "</details>"]
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text)


def main() -> int:
    entry = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    r = Result(entry)
    name = f"hatchery-smoke-{entry['id']}"
    logs = ""
    try:
        if pull(r):
            if entry.get("jupyter"):
                check_flags(r)
            ready = boot(r, name)
            logs = scan_logs(r, name) if sh(["docker", "inspect", name]).returncode == 0 else ""
            if ready and entry.get("jupyter"):
                check_notebooks(r, name)
    finally:
        sh(["docker", "rm", "-f", name])
    write_summary(r, logs)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "id": entry["id"], "name": entry["name"], "image": entry["image"],
            "sources": entry["sources"], "status": r.status, "notes": r.notes,
            "findings": [asdict(f) for f in r.findings.items],
        }, indent=2))
    # Findings are reported by report.py; a non-zero exit means the test itself broke.
    return 0


if __name__ == "__main__":
    sys.exit(main())
