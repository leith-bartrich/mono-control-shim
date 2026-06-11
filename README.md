# mono-control-shim

The thin, host-installed shim for **mono-control**.

This is the small piece of code that lives on your host machine. Its only job
is to locate the mono workspace and hand off to the real mono-control tooling
(which runs inside a dev container). The heavy lifting lives in `mono-control`,
not here — keeping the host footprint tiny and dependency-free is a deliberate
security goal.

## Design constraints

- **Standard library only.** No third-party dependencies, ever. The shim runs
  on the host with whatever trust the host has, so its dependency surface is
  kept at zero on purpose.
- **`uv`-installable.** Ships as a normal Python project with a console script.
- **Python 3.11+.**

## Install

```sh
uv tool install .
# or, from a checkout, for development:
uv tool install --editable .
```

This installs a `mono` command on your `PATH`.

## Usage

```sh
mono [--workspace PATH]
```

The workspace is resolved in this order:

1. The `--workspace` flag, if provided.
2. The `MONO_WORKSPACE` environment variable, if set.
3. Walking up from the current directory, looking for a directory that contains
   both `mono-control/` and `mono-config/` subdirectories.

If a workspace is found, the shim prints the resolved path and reports whether
mono-control's dev container is available. If no workspace can be found, it
prints an error and exits non-zero.

## Layout

```
mono-control-shim/
├── pyproject.toml          # stdlib-only, defines the `mono` entry point
├── README.md
└── mono_control_shim/
    ├── __init__.py
    └── cli.py              # argparse CLI, workspace resolution, container check
```
