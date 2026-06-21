# mono-control-shim

The thin, host-installed shim for **mono-control**.

This is the small piece of code that lives on your host machine. The heavy
lifting — managing repo state — happens in `mono-control`, which runs inside a
dev container. The shim's job is to bridge the host and that container while
keeping the host footprint tiny and dependency-free. That minimalism is a
deliberate security goal: less code on the host means a smaller attack surface.

## Role

- **Resolves the workspace location** (see [Commands](#commands) for the lookup order).
- **Bootstraps the workspace** — `mproj init` creates the `mono-config/` and
  `mono-repos/` directories.
- **Runs and operates on the mono-control artifact** in its container — `mproj
  control` to run it, plus `build-control` / `shell-control` / `test-control` for
  its image and container. Naming follows a deliberate convention; see
  [docs/design/command-conventions.md](docs/design/command-conventions.md).
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

## Commands

```sh
mproj                      # report the workspace and container availability
mproj init                 # bootstrap mono-config/ and mono-repos/
mproj control [args]       # run mono-control (args forward to its CLI; use -- for flags)
mproj build-control        # build the mono-control image (mono-control:latest)
mproj shell-control        # interactive shell in the mono-control container
mproj test-control [args]  # run mono-control's tests (dev only; args forward to pytest)
```

Every command also accepts `--workspace PATH`. The naming follows a deliberate
convention — `mproj <name>` runs an artifact, `mproj <name> <subcommand>`
forwards to the artifact's CLI, and `mproj <verb>-<name>` operates on its
container/image. Run and `shell-control` also take `--artifact` to force the
built image (artifact mode) instead of a live checkout. Full rationale:
[docs/design/command-conventions.md](docs/design/command-conventions.md).

### Workspace resolution

The workspace is resolved in this order:

1. The `--workspace` flag, if provided.
2. The `MONO_WORKSPACE` environment variable, if set.
3. Walking up from the current directory for a directory that contains a
   `mono-config/` subdirectory.

A workspace is defined by its `mono-config/` manifest dir; a sibling
`mono-control/` checkout is optional and selects dev vs. artifact execution. If
no workspace is found, the shim prints an error and exits non-zero.

## Layout

```
mono-control-shim/
├── pyproject.toml          # stdlib-only, defines the `mproj` entry point
├── README.md
├── docs/
│   └── design/
│       └── command-conventions.md   # the mproj command-naming pattern
└── mono_control_shim/
    ├── __init__.py
    └── cli.py              # argparse CLI: workspace resolution + artifact ops
```
