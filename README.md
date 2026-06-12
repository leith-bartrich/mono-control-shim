# mono-control-shim

The thin, host-installed shim for **mono-control**.

This is the small piece of code that lives on your host machine. The heavy
lifting — managing repo state — happens in `mono-control`, which runs inside a
dev container. The shim's job is to bridge the host and that container while
keeping the host footprint tiny and dependency-free. That minimalism is a
deliberate security goal: less code on the host means a smaller attack surface.

## Role

- **Resolves the workspace location** (see [Usage](#usage) for the lookup order).
- **Bootstraps the workspace** — `mproj init` creates the `mono-config/` and
  `mono-repos/` directories.
- **Checks dev container availability** for mono-control.
- **Future: a gateway for host-level operations** that genuinely cannot run
  inside the container (e.g. native Windows builds). This is the exception, not
  the default — containerized execution stays preferred for security.

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

This installs a `mproj` command on your `PATH`.

## Usage

```sh
mproj [--workspace PATH]
mproj init [--workspace PATH]   # bootstrap mono-config/ and mono-repos/
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
├── pyproject.toml          # stdlib-only, defines the `mproj` entry point
├── README.md
└── mono_control_shim/
    ├── __init__.py
    └── cli.py              # argparse CLI, workspace resolution, container check
```
