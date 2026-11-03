# Hatchery config validation

Run by `.github/workflows/hatchery_config_validation.yaml`, independently of
the integration tests. It is an **advisory** check: it is not required for
merging, and problems do not fail the run. A red run means the checker
itself broke.

**When it runs.** GitHub path filters cannot look inside files, so the
workflow starts on any PR touching a `values/*.yaml` file. It then compares
each file's hatchery section with the base branch and stops after a few
seconds if none changed. Only workspace containers whose boot-relevant fields
(image, command, args, env, uid/gid, probe, hooks, volume locations) changed
are booted. Changes to the validator itself do not trigger it. Running the
workflow by hand (Actions → Run workflow) checks every config; that is the
only way to get a full run.

**Where results show up.**

- Inline on the PR's *Files changed* tab, on the offending line of the values
  file, with what is wrong and how to fix it.
- One PR comment, updated on every push, combining all checks. Fork PRs get a
  read-only token, so for those the same report is only in the run's job
  summary.
- Each Jupyter job's summary has the details for that workspace, including
  the container log when it failed to boot.

To make problems fail the run (e.g. before making it a required check), set
`ADVISORY: "false"` at the top of the workflow.

## What is checked

**Static (`check_config.py`)**, for every changed config:

- `hatchery.hatchery.json` is valid JSON with no duplicate keys
- required container fields, Kubernetes CPU/memory quantities, and the
  `pull_policy` / `use-tls` / `use-shared-memory` values hatchery silently
  falls back on when misspelled
- `ready-probe` is under the Jupyter `base_url`, `base_url` matches
  `path-rewrite`, Jupyter flags are well formed and not set twice
- `FRAME_ANCESTORS`, `allow_origin` and the sidecar `HOSTNAME` match the
  commons' `global.hostname` (catches values copied from another commons)

**Hatchery's own loader (`loader/`)**: each rendered config is walked
alongside hatchery's Go types, and every unknown key is reported (hatchery
itself ignores them, so a typo deploys and silently does nothing). It is then
passed to `hatchery.LoadConfig`, which is what the service runs at startup;
a type error there means hatchery would crash-loop.

**Docker smoke test (`smoke_test.py`)**, one job per new or changed container:

1. checks every `--Class.trait=value` flag against the Jupyter installed in
   the image (unknown class, unknown trait, bad value, deprecated trait);
   Jupyter itself ignores unknown classes without a word
2. runs the container with the configured command, args, env, uid and
   fs-gid, with empty volumes where hatchery mounts them, runs
   `lifecycle-post-start`, and waits for the `ready-probe` to answer 2xx/3xx
3. fails on Jupyter log lines like ``Config option `x` not recognized`` and
   critical errors
4. validates every notebook baked into the image with `nbformat`, warns
   about notebooks whose kernel is not installed, and checks that a
   `default_url` such as `/lab/tree/welcome.ipynb` exists

Notebooks are not executed: most need data access or credentials.

## Private ECR images

Images in `*.dkr.ecr.*.amazonaws.com` need registry credentials. Set the
repository variable `HATCHERY_ECR_ROLE_ARN` to an IAM role that trusts this
repo via GitHub OIDC and can pull from ECR. Without it, those containers are
skipped with a warning instead of failing.

## Running locally

```sh
pip install pyyaml
python3 .github/hatchery/check_config.py --all --out build          # or --base origin/master

git clone https://github.com/uc-cdis/hatchery .hatchery-src
(cd .github/hatchery/loader && go mod tidy && go run . "$PWD/../../../build/manifest.json" "$PWD/../../../build/findings/loader.json")

jq '.[0]' build/matrix.json > entry.json
python3 .github/hatchery/smoke_test.py entry.json build/smoke/0.json

python3 .github/hatchery/report.py build report.md   # the PR comment
```

## Files

| File | Role |
|---|---|
| `check_config.py` | renders each commons' hatchery.json, static checks, smoke-test matrix |
| `loader/` | Go: unknown keys and `hatchery.LoadConfig`, using hatchery's own types |
| `smoke_test.py` | boots one workspace container in Docker |
| `in_image/` | scripts run inside the workspace image with its own Python (flags, notebooks) |
| `findings.py` | shared finding format, line lookup and GitHub annotations |
| `report.py` | combines everything into the PR comment and job summary |
