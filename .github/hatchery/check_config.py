#!/usr/bin/env python3
"""Static validation of hatchery workspace configs in this repo.

For every values file that configures hatchery (``hatchery.hatchery.json`` or
``hatchery.hatchery.containers``) this script:

* renders the effective hatchery.json the chart puts in the manifest-hatchery
  ConfigMap and writes it under ``--out`` for the Go loader, which validates it
  with hatchery's own types;
* runs checks hatchery itself does not do: duplicate JSON keys, resource
  quantities, enum-like strings hatchery silently falls back on, probe and
  base_url consistency, hostnames copied from another commons, and the shape
  of the Jupyter command-line flags;
* builds the matrix of containers for the Docker smoke test. With ``--base``
  only containers that are new or changed relative to that ref are included,
  so a PR touching one workspace image only boots that image.

Findings are printed as GitHub annotations on the offending line and written
to ``--out``/findings/static.json for report.py. The exit status is 0 even
when there are findings; non-zero means the checker itself failed.

    check_config.py --all --out build/
    check_config.py --base origin/master --out build/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from findings import Findings  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# Defaults the chart fills in when hatchery is configured with YAML
# (`hatchery.containers`) instead of a raw `hatchery.json` string. Mirrors
# gen3-helm/helm/hatchery/templates/hatchery-manifest.yaml.
CHART_DEFAULT_SIDECAR = {
    "cpu-limit": "0.1",
    "memory-limit": "256Mi",
    "image": "quay.io/cdis/ecs-ws-sidecar:master",
    "env": {"NAMESPACE": "{{ .Release.Namespace }}", "HOSTNAME": "{{ .Values.global.hostname }}"},
    "args": [],
    "command": ["/bin/bash", "./sidecar.sh"],
    "lifecycle-pre-stop": ["su", "-c", "echo test", "-s", "/bin/sh", "root"],
}

CPU_RE = re.compile(r"^(\d+(\.\d+)?|\d+m)$")
MEM_RE = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|k|K|M|G|T|P)?$")
# --Class.trait=value  (traitlets long form)
TRAIT_FLAG_RE = re.compile(r"^--([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
# --flag or --alias=value (e.g. --no-browser, --port=8888, --ip=0.0.0.0)
ALIAS_FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]*(=.*)?$", re.S)
ECR_RE = re.compile(r"^\d+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/")

# Fields that change how the workspace boots. A container is re-tested when any
# of them changes; edits to e.g. the display name or cpu-limit alone are not
# worth pulling a multi-GB image for.
BOOT_FIELDS = (
    "image", "command", "args", "env", "target-port", "ready-probe", "user-uid",
    "group-uid", "fs-gid", "lifecycle-post-start", "use-shared-memory",
    "user-volume-location", "gen3-volume-location",
)


@dataclass
class Target:
    path: str  # repo-relative values file
    config: dict
    hostname: str | None
    yaml_form: bool
    problems: list[str] = field(default_factory=list)  # parse-level errors


class DuplicateKeyError(ValueError):
    pass


def _no_duplicates(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            # Go keeps the last value silently; that is almost always a merge
            # or copy/paste mistake.
            raise DuplicateKeyError(f"duplicate key {k!r}")
        seen[k] = v
    return seen


def load_target(path: str, text: str) -> Target | None:
    """Return the effective hatchery config of a values file, or None if the
    file does not configure hatchery."""
    try:
        values = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return Target(path, {}, None, False, [f"values file is not valid YAML: {e}"])
    if not isinstance(values, dict):
        return None
    chart = values.get("hatchery")
    if not isinstance(chart, dict):
        return None
    hatch = chart.get("hatchery")
    if not isinstance(hatch, dict):
        return None
    hostname = (values.get("global") or {}).get("hostname")

    raw = hatch.get("json")
    if raw:
        try:
            config = json.loads(raw, object_pairs_hook=_no_duplicates)
        except DuplicateKeyError as e:
            return Target(path, {}, hostname, False, [f"hatchery.hatchery.json has a {e}"])
        except json.JSONDecodeError as e:
            return Target(path, {}, hostname, False, [f"hatchery.hatchery.json is not valid JSON: {e}"])
        if not isinstance(config, dict):
            return Target(path, {}, hostname, False, ["hatchery.hatchery.json must be a JSON object"])
        return Target(path, config, hostname, False)

    if "containers" not in hatch:
        return None
    # YAML form: reproduce what the chart template renders.
    config = {
        "user-namespace": "jupyter-pods",
        "sub-dir": "/lw-workspace",
        "user-volume-size": "10Gi",
        "sidecar": hatch.get("sidecarContainer", CHART_DEFAULT_SIDECAR),
        "containers": hatch.get("containers") or [],
    }
    return Target(path, config, hostname, True)


def is_placeholder(value: str) -> bool:
    return "{{" in value or "<" in value


def jupyter_flags(args: list[str]) -> dict[str, str]:
    """Map 'Class.trait' -> value for traitlets-style args."""
    out = {}
    for a in args:
        m = TRAIT_FLAG_RE.match(a)
        if m:
            out[f"{m.group(1)}.{m.group(2)}"] = m.group(3)
    return out


def strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def looks_like_jupyter(c: dict) -> bool:
    cmd = " ".join(c.get("command") or [])
    return bool(jupyter_flags(c.get("args") or [])) or any(
        k in cmd for k in ("jupyter", "notebook", "jupyterlab")
    )


def check_container(t: Target, c: dict, idx: int, report: Findings) -> None:
    f = t.path
    name = c.get("name") or f"containers[{idx}]"

    def err(check, msg, hint="", needle=()):
        report.error(check, f, msg, hint=hint, container=name, needle=needle)

    def warn(check, msg, hint="", needle=()):
        report.warning(check, f, msg, hint=hint, container=name, needle=needle)

    for key in ("name", "image", "target-port", "cpu-limit", "memory-limit", "ready-probe"):
        if c.get(key) in (None, ""):
            err("Missing field", f"required field {key!r} is missing or empty.",
                f'add "{key}" to this container; see another workspace in this file for an example.')

    image = c.get("image") or ""
    if image and ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
        err("Image tag", f"image {image!r} has no tag, so every launch may get a different image.",
            "pin a released tag, e.g. `:3.5.0`.", needle=image)
    elif image.endswith((":latest", ":master")):
        warn("Image tag", f"image {image!r} uses a moving tag; workspaces can change without a PR here.",
             "pin a released tag when one exists.", needle=image)

    cpu = str(c.get("cpu-limit", ""))
    if cpu and not CPU_RE.match(cpu):
        err("Resources", f"cpu-limit {cpu!r} is not a Kubernetes CPU quantity, so the pod will be rejected.",
            'use cores ("2.0") or millicores ("500m").', needle="cpu-limit")
    mem = str(c.get("memory-limit", ""))
    if mem and not MEM_RE.match(mem):
        err("Resources", f"memory-limit {mem!r} is not a Kubernetes memory quantity, so the pod will be rejected.",
            'use a binary unit, e.g. "4Gi" or "512Mi".', needle="memory-limit")
    elif mem and re.match(r"^\d+(\.\d+)?$", mem):
        err("Resources", f"memory-limit {mem!r} has no unit, which Kubernetes reads as {mem} bytes; the workspace would be OOM-killed at start.",
            f'add a unit, e.g. "{mem}Gi".', needle="memory-limit")

    # Hatchery silently falls back for these, so a typo is never reported.
    pp = c.get("pull_policy")
    if pp is not None and pp not in ("Always", "IfNotPresent", "Never"):
        err("Pull policy", f"pull_policy {pp!r} is not recognised; hatchery silently uses IfNotPresent instead.",
            'use "Always", "IfNotPresent" or "Never" (case-sensitive).', needle="pull_policy")
    for key in ("use-tls", "use-shared-memory"):
        v = c.get(key)
        if v is not None and v not in ("true", "false"):
            err("Field type", f"{key} is {v!r}; hatchery only acts on the exact string \"true\".",
                f'set "{key}": "true" or "false" (quoted).', needle=key)

    probe = c.get("ready-probe") or ""
    rewrite = c.get("path-rewrite") or ""
    if probe and not probe.startswith("/"):
        err("Probe", f"ready-probe {probe!r} is not an absolute path.", 'start it with "/", e.g. "/lw-workspace/proxy/".', needle="ready-probe")
    if rewrite and not (rewrite.startswith("/") and rewrite.endswith("/")):
        err("Probe", f"path-rewrite {rewrite!r} must start and end with '/'.", 'e.g. "/lw-workspace/proxy/".', needle="path-rewrite")

    for key in ("user-volume-location", "gen3-volume-location"):
        v = c.get(key)
        if v and not v.startswith("/"):
            err("Volumes", f"{key} {v!r} is not an absolute path.", 'e.g. "/home/jovyan/pd".', needle=key)

    for key in ("command", "args", "lifecycle-post-start", "lifecycle-pre-stop"):
        v = c.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            err("Field type", f"{key} must be a list of strings; hatchery will fail to load the whole config.",
                f'write it as ["..."], quoting every element.', needle=key)

    env = c.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        bad = [k for k, v in env.items() if not isinstance(v, str)] if isinstance(env, dict) else []
        err("Field type", f"env values must be strings{' (not: ' + ', '.join(bad) + ')' if bad else ''}; hatchery will fail to load the whole config.",
            'quote numbers and booleans, e.g. "PORT": "8888".', needle=bad[0] if bad else "env")
        env = {}

    sq = c.get("squashfs_mount") or {}
    if sq.get("enabled"):
        if not sq.get("source_sqsh"):
            err("Software library", "squashfs_mount is enabled but source_sqsh is empty, so nothing will be mounted at /apps.",
                'set source_sqsh, e.g. "/image/apps-2026-09-08.sqsh", or disable the mount.', needle="squashfs_mount")
        if not (sq.get("bucket_name") or (t.config.get("s3-config") or {}).get("bucketName")):
            err("Software library", "squashfs_mount is enabled but no S3 bucket is configured.",
                'set s3-config.bucketName for the commons (or squashfs_mount.bucket_name for this workspace).', needle="squashfs_mount")

    # Hostnames copied from another commons.
    host = t.hostname
    if host and not is_placeholder(host):
        fa = env.get("FRAME_ANCESTORS")
        if fa and not is_placeholder(fa) and host not in fa:
            err("Hostname", f"FRAME_ANCESTORS is {fa!r} but this commons is {host!r}; the portal cannot embed the workspace.",
                f'set FRAME_ANCESTORS to "https://{host}". This often happens when a block is copied from another commons.', needle="FRAME_ANCESTORS")

    # Jupyter flags.
    args = c.get("args") or []
    if not looks_like_jupyter(c):
        return
    flags = {}
    for a in args:
        if not a.startswith("--"):
            continue
        m = TRAIT_FLAG_RE.match(a)
        if m:
            key = f"{m.group(1)}.{m.group(2)}"
            if key in flags and flags[key] != m.group(3):
                err("Jupyter flag", f"--{key} is set twice with different values; only the last one takes effect.",
                    "remove one of them.", needle=a)
            flags[key] = m.group(3)
        elif "." in a.split("=", 1)[0]:
            err("Jupyter flag", f"malformed flag {a!r}; Jupyter ignores it.", "use the form --Class.trait=value.", needle=a)
        elif not ALIAS_FLAG_RE.match(a):
            err("Jupyter flag", f"unrecognised argument {a!r}.", "use --Class.trait=value or a documented alias such as --no-browser.", needle=a)

    for key, value in flags.items():
        trait = key.split(".")[1]
        v = strip_quotes(value)
        arg = f"--{key}={value}"
        if trait == "base_url" and rewrite and v.rstrip("/") + "/" != rewrite:
            err("Jupyter flag", f"{arg} does not match path-rewrite {rewrite!r}; requests through the workspace proxy will 404.",
                f"make them identical, e.g. --{key}={rewrite}", needle=arg)
        if trait == "allow_origin" and host and not is_placeholder(host) and not is_placeholder(v) and v not in ("*", "") and host not in v:
            err("Hostname", f"{arg} does not match this commons ({host!r}); the browser will block requests from the portal.",
                f"use --{key}='https://{host}'. This often happens when a block is copied from another commons.", needle=arg)
        if trait in ("root_dir", "notebook_dir") and v and not v.startswith("/"):
            err("Jupyter flag", f"{arg} is not an absolute path.", "e.g. '/home/jovyan'.", needle=arg)

    base_urls = {strip_quotes(v) for k, v in flags.items() if k.endswith(".base_url")}
    if probe and base_urls and not any(probe.startswith(b) for b in base_urls):
        err("Probe", f"ready-probe {probe!r} is outside Jupyter's base_url {sorted(base_urls)}; the probe will 404 and the workspace will never show as ready.",
            f"point the probe under the base_url, e.g. \"{sorted(base_urls)[0]}\".", needle="ready-probe")
    classes = {k.split(".")[0] for k in flags}
    if "NotebookApp" in classes and "ServerApp" in classes:
        warn("Jupyter flag", "mixes --NotebookApp.* and --ServerApp.* flags; on Jupyter Server 2 the NotebookApp ones only work through the deprecated notebook_shim.",
             "rename --NotebookApp.X to --ServerApp.X (the Jupyter smoke test lists the exact replacements).", needle="--NotebookApp.")


def check_target(t: Target, report: Findings) -> None:
    for p in t.problems:
        report.error("Parse", t.path, p, hint="fix the syntax; hatchery would fail to start with this config.")
    if t.problems:
        return
    containers = t.config.get("containers")
    if not isinstance(containers, list) or not containers:
        report.error("Missing field", t.path, "no workspace containers are configured, so users have nothing to launch.",
                     hint='add at least one entry under "containers".')
        return
    names = [c.get("name") for c in containers if isinstance(c, dict)]
    for n in sorted({n for n in names if names.count(n) > 1}):
        report.warning("Duplicate name", t.path, f"{names.count(n)} workspaces are named {n!r}; users cannot tell them apart in the launcher.",
                       hint="give each a distinct name, e.g. include the CPU/RAM size.", container=n)
    for i, c in enumerate(containers):
        if not isinstance(c, dict):
            report.error("Field type", t.path, f"containers[{i}] is not an object.")
            continue
        check_container(t, c, i, report)

    pricing = t.config.get("pricing")
    if pricing is not None:
        for k in ("cpu", "memory"):
            v = pricing.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                report.error("Pricing", t.path, f"pricing.{k} is {v!r}; it must be a non-negative number (hourly rate).",
                             hint='write it unquoted, e.g. "cpu": 0.04048.', needle='"pricing"')

    sidecar = t.config.get("sidecar") or {}
    host = t.hostname
    sh = (sidecar.get("env") or {}).get("HOSTNAME")
    if host and sh and not is_placeholder(host) and not is_placeholder(sh) and sh != host:
        report.error("Hostname", t.path, f"the sidecar's HOSTNAME is {sh!r} but this commons is {host!r}; workspace data access will go to the wrong commons.",
                     hint=f'set sidecar.env.HOSTNAME to "{host}" (check GEN3_ENDPOINT too). This often happens when a block is copied from another commons.',
                     needle=f'"HOSTNAME": "{sh}"')


def boot_key(c: dict) -> str:
    subset = {k: c.get(k) for k in BOOT_FIELDS}
    return hashlib.sha256(json.dumps(subset, sort_keys=True).encode()).hexdigest()[:12]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def candidate_files() -> list[str]:
    out = git("ls-files", "*.yaml", "*.yml")
    return [p for p in out.splitlines() if "/values/" in p and not p.startswith(".github/")]


def read_at(ref: str, path: str) -> str | None:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="validate every hatchery config and smoke-test every container")
    mode.add_argument("--base", help="git ref to diff against; only changed files/containers are checked")
    ap.add_argument("--smoke-all", action="store_true", help="with --base: still put every container in the smoke matrix")
    ap.add_argument("--out", required=True, type=Path, help="directory for rendered configs, manifest.json and matrix.json")
    args = ap.parse_args()

    if args.base:
        merge_base = git("merge-base", "HEAD", args.base).strip()
        changed = set(git("diff", "--name-only", "--diff-filter=ACMRT", f"{merge_base}...HEAD").splitlines())
        files = [p for p in candidate_files() if p in changed]
    else:
        merge_base = None
        files = candidate_files()

    report = Findings(prefix="Hatchery config")
    out: Path = args.out
    (out / "configs").mkdir(parents=True, exist_ok=True)
    manifest, matrix = [], {}

    for path in files:
        t = load_target(path, (REPO / path).read_text())
        if t is None:
            continue
        if merge_base and not args.smoke_all:
            old_text = read_at(merge_base, path)
            old = load_target(path, old_text) if old_text else None
            if old is not None and not old.problems and old.config == t.config:
                continue  # file changed, but not its hatchery config
            old_keys = {boot_key(c) for c in (old.config.get("containers") or []) if isinstance(c, dict)} if old and not old.problems else set()
        else:
            old_keys = set()

        print(f"checking {path}")
        check_target(t, report)
        if t.problems:
            continue

        rendered = out / "configs" / (path.replace("/", "__") + ".json")
        rendered.write_text(json.dumps(t.config, indent=2))
        manifest.append({"source": path, "config": str(rendered.resolve())})

        for c in t.config.get("containers") or []:
            if not isinstance(c, dict) or not c.get("image"):
                continue
            key = boot_key(c)
            if key in old_keys:
                continue
            entry = matrix.setdefault(key, {
                "id": key,
                "name": c.get("name", ""),
                "image": c["image"],
                "private": bool(ECR_RE.match(c["image"])),
                "jupyter": looks_like_jupyter(c),
                "container": c,
                "sources": [],
            })
            entry["sources"].append(path)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    items = sorted(matrix.values(), key=lambda e: (e["image"], e["id"]))
    for e in items:
        # Short label for the job name in the Actions UI.
        e["label"] = f'{e["image"].rsplit("/", 1)[-1]} ({e["sources"][0].split("/")[0]})'
    (out / "matrix.json").write_text(json.dumps(items))
    report.write(out / "findings" / "static.json")
    (out / "checked.json").write_text(json.dumps({"files": [m["source"] for m in manifest], "containers": len(items)}))

    if not manifest:
        print("\nNo hatchery config changed in this PR; nothing to check.")
    else:
        print(f"\n{len(manifest)} hatchery config(s) checked, {len(items)} container(s) queued for the Jupyter smoke test, "
              f"{report.errors} error(s), {report.warnings} warning(s)")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"configs={len(manifest)}\n")
            fh.write(f"matrix={json.dumps({'include': items})}\n")
            fh.write(f"has_containers={'true' if items else 'false'}\n")
    # Findings are reported by report.py; a non-zero exit means the checker broke.
    return 0


if __name__ == "__main__":
    sys.exit(main())
