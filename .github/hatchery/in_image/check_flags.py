"""Check Jupyter command-line flags against the Jupyter installed in this image.

Runs inside the workspace image, with the image's own Python, so it sees the
exact jupyter_server / notebook / jupyterlab versions users get. Jupyter only
warns about an unknown trait on a known class, and says nothing at all about
an unknown class (a typo, or --NotebookApp.* on Jupyter Server 2), so a broken
flag otherwise goes unnoticed.

Usage: python check_flags.py ARG [ARG...]
Prints one JSON object: {"versions": {...}, "results": [{"arg", "level", "message"}]}
Kept compatible with old Pythons (3.6+) because some images are old.
"""
import difflib
import importlib
import json
import re
import sys

FLAG_RE = re.compile(r"^--([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)

MODULES = [
    "jupyter_server.serverapp",
    "jupyterlab.labapp",
    "notebook.app",          # Notebook 7 (JupyterNotebookApp)
    "notebook.notebookapp",  # Notebook <= 6 (NotebookApp)
    "nbclassic.notebookapp",
    "jupyter_server.services.contents.filemanager",
    "jupyter_server.services.contents.largefilemanager",
    "jupyter_server.auth.identity",
    "jupyter_server.services.kernels.kernelmanager",
    "jupyter_client.kernelspec",
    "jupyter_resource_usage",
    "notebook_shim.shim",
]


def all_subclasses(cls):
    out, stack = {}, [cls]
    while stack:
        c = stack.pop()
        for s in c.__subclasses__():
            if s.__name__ not in out:
                out[s.__name__] = s
                stack.append(s)
    return out


def main(args):
    versions, results = {}, []
    try:
        from traitlets import TraitError
        from traitlets.config import Configurable
    except ImportError:
        print(json.dumps({"versions": {}, "results": [{"arg": "", "level": "error", "message": "traitlets is not importable with this Python; is Jupyter installed?"}]}))
        return
    for mod in MODULES:
        try:
            importlib.import_module(mod)
        except Exception:
            continue
    for pkg in ("jupyter_server", "jupyterlab", "notebook", "nbclassic", "traitlets"):
        try:
            versions[pkg] = importlib.import_module(pkg).__version__
        except Exception:
            pass

    classes = all_subclasses(Configurable)

    # notebook_shim (used by Notebook 7 and some JupyterLab versions) reroutes
    # traits an extension app does not have to ServerApp, or to the old
    # NotebookApp traits, with a "has moved" warning. Those flags work today
    # but stop working when the shim is removed.
    shim_mixin = shim_traits = None
    try:
        from notebook_shim.shim import NotebookConfigShimMixin as shim_mixin
        from notebook_shim.traits import NotebookAppTraits as shim_traits
    except Exception:
        pass
    server_app = classes.get("ServerApp")

    def forwarded(cls_name, cls, trait_name):
        """Return (target class name, trait) if notebook_shim forwards it."""
        if shim_mixin is None:
            return None
        shimmed = cls_name == "NotebookApp" or (cls is not None and issubclass(cls, shim_mixin))
        if not shimmed:
            return None
        if server_app is not None and trait_name in server_app.class_traits(config=True):
            return "ServerApp", server_app.class_traits(config=True)[trait_name]
        if shim_traits is not None and trait_name in shim_traits.class_traits(config=True):
            return "NotebookApp", shim_traits.class_traits(config=True)[trait_name]
        return None

    for arg in args:
        m = FLAG_RE.match(arg)
        if not m:
            continue
        cls_name, trait_name, raw = m.groups()
        cls = classes.get(cls_name)
        traits = cls.class_traits(config=True) if cls is not None else {}
        trait = traits.get(trait_name)
        target = "%s.%s" % (cls_name, trait_name)
        moved = None
        if trait is None:
            fwd = forwarded(cls_name, cls, trait_name)
            if fwd is not None:
                target = "%s.%s" % (fwd[0], trait_name)
                trait = fwd[1]
                moved = ("forwarded to %s by notebook_shim, which is a deprecated transition shim; "
                         "use --%s=... instead" % (target, target))
            elif cls is None:
                results.append({"arg": arg, "level": "error",
                                "message": "no Jupyter class named %r in this image; Jupyter ignores the flag without telling you" % cls_name})
                continue
            else:
                close = difflib.get_close_matches(trait_name, list(traits), n=1)
                hint = " (did you mean %s?)" % close[0] if close else ""
                results.append({"arg": arg, "level": "error",
                                "message": "%s has no configurable trait %r%s" % (cls_name, trait_name, hint)})
                continue
        # Same conversion traitlets applies to command-line values.
        try:
            value = trait.from_string(raw) if hasattr(trait, "from_string") else raw
        except (TraitError, ValueError, TypeError) as e:
            results.append({"arg": arg, "level": "error",
                            "message": "invalid value for %s: %s" % (target, e)})
            continue
        try:
            trait.validate(None, value)
        except TraitError as e:
            results.append({"arg": arg, "level": "error",
                            "message": "invalid value for %s: %s" % (target, e)})
            continue
        except Exception:
            pass  # some validators need a real instance; Jupyter's boot will catch those
        notes = [moved] if moved else []
        help_text = (trait.help or "").strip()
        if help_text.upper().startswith("DEPRECATED"):
            notes.append("%s is deprecated: %s" % (target, help_text.splitlines()[0]))
        if notes:
            results.append({"arg": arg, "level": "warning", "message": "; ".join(notes)})
        else:
            results.append({"arg": arg, "level": "ok", "message": target})

    print(json.dumps({"versions": versions, "results": results}))


if __name__ == "__main__":
    main(sys.argv[1:])
