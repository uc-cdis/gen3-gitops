"""Validate the notebooks baked into a workspace image.

Runs inside the running workspace container, with the image's own Python:

* every .ipynb under the Jupyter root dir (and $HOME) must parse and pass
  nbformat schema validation;
* each notebook's kernelspec should be installed in the image, otherwise users
  get "kernel not found" when they open it;
* the default_url (e.g. /lab/tree/welcome.ipynb) must point at a file that
  exists, otherwise the workspace opens on a 404.

Notebooks are not executed: most need data access or credentials.

Usage: python check_notebooks.py ROOT_DIR [DEFAULT_URL]
Prints one JSON object: {"checked": N, "results": [{"path", "level", "message"}]}
Kept compatible with old Pythons (3.6+).
"""
import json
import os
import sys

SKIP_DIRS = {".ipynb_checkpoints", "site-packages", "node_modules", ".git", ".cache", ".local", "pd", "proc", "sys"}
MAX_NOTEBOOKS = 500


def find_notebooks(roots):
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".ipynb"):
                    p = os.path.realpath(os.path.join(dirpath, fn))
                    if p not in seen:
                        seen.add(p)
                        yield os.path.join(dirpath, fn)
                        if len(seen) >= MAX_NOTEBOOKS:
                            return


def default_url_target(default_url):
    for prefix in ("/lab/tree/", "/tree/", "/notebooks/", "/doc/tree/"):
        if default_url.startswith(prefix):
            return default_url[len(prefix):]
    return None


def main(argv):
    root = argv[0] if argv else os.path.expanduser("~")
    default_url = argv[1] if len(argv) > 1 else ""
    results = []

    try:
        import nbformat
    except ImportError:
        print(json.dumps({"checked": 0, "results": [{"path": "", "level": "warning", "message": "nbformat is not importable; notebooks not validated"}]}))
        return
    try:
        from jupyter_client.kernelspec import KernelSpecManager
        kernels = set(KernelSpecManager().find_kernel_specs())
    except Exception:
        kernels = None

    checked = 0
    for path in find_notebooks([root, os.path.expanduser("~")]):
        checked += 1
        try:
            nb = nbformat.read(path, as_version=4)
            nbformat.validate(nb)
        except Exception as e:
            msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            results.append({"path": path, "level": "error", "message": "invalid notebook: %s" % msg})
            continue
        ks = (nb.get("metadata") or {}).get("kernelspec") or {}
        name = ks.get("name")
        if kernels is not None and name and name not in kernels:
            results.append({"path": path, "level": "warning",
                            "message": "uses kernel %r, which is not installed (have: %s)" % (name, ", ".join(sorted(kernels)) or "none")})

    target = default_url_target(default_url) if default_url else None
    if target:
        full = os.path.join(root, target)
        if not os.path.exists(full):
            results.append({"path": full, "level": "error",
                            "message": "default_url %s points at a file that does not exist in the image" % default_url})

    print(json.dumps({"checked": checked, "results": results}))


if __name__ == "__main__":
    main(sys.argv[1:])
