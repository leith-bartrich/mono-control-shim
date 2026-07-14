# mono-control-shim

## The mono repository control system

This project is part of a system for managing development with a **mono
repository** approach — many repos and their configuration treated as one
coordinated workspace. `mono-control-shim` is the top-level command shim and
entrypoint to that system: the `mproj` command you run on your host. Over time
it will grow to better orchestrate the system and its sub-components, but it
stays deliberately thin. Because the shim is the one piece that executes on the
host, it is heavily dependent on reducing supply-chain dependencies — every
dependency it takes on is one the host inherits. The heavy lifting lives
in containerized artifacts (today, `mono-control`); the shim's job is to locate
the workspace and hand off to them.

## The shim

The thin, host-installed shim for **mono-control**.

This is the small piece of code that lives on your host machine. The heavy
lifting — managing repo state — happens in `mono-control`, which runs inside a
dev container. The shim's job is to bridge the host and that container while
keeping the host footprint tiny and dependency-free. That minimalism is a
deliberate security goal: less code on the host means a smaller attack surface.

## Role

- **Resolves the workspace location** (see [Commands](#commands) for the lookup order).
- **Bootstraps the workspace** — `mproj init` creates the `mono-config/`,
  `mono-repos/` and `mono-repos-offline/` directories (all host-side bind-mount
  sources the container needs).
- **Runs and operates on the mono-control artifact** in its container — `mproj
  control` to run it, plus `build-control` / `shell-control` / `test-control` for
  its image and container. Naming follows a deliberate convention; see
  [docs/design/command-conventions.md](docs/design/command-conventions.md).
- **Declares the host platform to the container.** The shim is the host-side
  authority on which OS it runs on, so on every container run it detects the host
  and passes it in as `MONO_CONTROL_HOST_PLATFORM` (`windows` / `darwin` /
  `linux`), which mono-control consumes. Setting that variable in the environment
  overrides detection (useful for exercising another platform's behavior).
- **Supplies the GitHub credential to the container.** Cloning private repos needs
  a token, and the container cannot get one — your host's lives in an OS keyring no
  Linux container can reach. The shim resolves one host-side and hands it in as
  `MONO_CONTROL_GITHUB_TOKEN` (see [GitHub token](#github-token)).
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
mproj init                 # bootstrap mono-config/, mono-repos/ and mono-repos-offline/
mproj control [args]       # run mono-control (args forward to its CLI; use -- for flags)
mproj build-control        # build the mono-control image (mono-control:latest)
mproj shell-control        # interactive shell in the mono-control container
mproj test-control [args]  # run mono-control's tests (dev only; args forward to pytest)
```

Every command also accepts `--workspace PATH`. The naming follows a deliberate
convention — `mproj <name>` runs an artifact, `mproj <name> <subcommand>`
forwards to the artifact's CLI, and `mproj <verb>-<name>` operates on its
container/image. Run and `shell-control` also take `--artifact` to force the
built image (artifact mode) instead of a live checkout. In dev mode, `mproj
control --build` rebuilds the dev image (via Compose) before running, to pick up
mono-control source or dependency changes — distinct from `build-control`, which
builds the standalone `mono-control:latest` artifact image. Full rationale:
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

### GitHub token

Managed repos are usually private, so mono-control needs a credential to clone them.
The shim resolves one on every container run, in this order:

1. `MONO_CONTROL_GITHUB_TOKEN`, if set.
2. `GH_TOKEN`, then `GITHUB_TOKEN` (the ecosystem's conventions).
3. `gh auth token` — your existing `gh` login. **A warning is printed when this is
   used.**
4. Nothing. This is *not* an error: public remotes need no credential, and most of
   mono-control needs no network. A private remote with no token then fails inside
   the container with a message naming both ways out.

**Prefer a scoped token.** mono-control never writes to a remote — it only clones,
lists refs, and checks out — so a **fine-grained PAT with read-only Contents, limited
to the repos you manage**, is all it needs. Your `gh` OAuth token, by contrast, carries
`repo` + `workflow` + `gist` *write* access to every repo you own, and `workflow` reaches
your GitHub Actions secrets. Export the scoped one and the fallback never fires:

```sh
export MONO_CONTROL_GITHUB_TOKEN=github_pat_...
```

The token is passed to `docker` by **name only** (a valueless `-e`), with the value
carried in the environment — so it never appears in this process's `argv`, where any
local process could read it off the process table. It is never printed. Inside the
container a credential helper supplies it to git from the environment, scoped to
`github.com` alone, and it is never written to disk. Full rationale: mono-control's
[docs/design/github-auth.md](https://github.com/leith-bartrich/mono-control/blob/master/docs/design/github-auth.md).

## Tests

Stdlib `unittest` — a test framework is a dependency like any other, and this repo
takes none:

```sh
python -m unittest discover -s tests -t .
```

## Layout

```
mono-control-shim/
├── pyproject.toml          # stdlib-only, defines the `mproj` entry point
├── README.md
├── docs/
│   └── design/
│       └── command-conventions.md   # the mproj command-naming pattern
├── tests/
│   └── test_cli.py         # stdlib unittest; token resolution + secret plumbing
└── mono_control_shim/
    ├── __init__.py
    └── cli.py              # argparse CLI: workspace resolution + artifact ops
```
