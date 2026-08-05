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
  `mono-repos-bare/` and `mono-work/` directories (host-side dirs the broker acts
  on: the bare repositories and the worktrees they are materialized into), and
  populates `mono-config/` by cloning an existing config repo or creating a fresh
  one. The shim has to own that checkout: `mono-config/` is the marker that makes a
  directory a workspace, so nothing downstream — including mono-control, which
  never touches the host filesystem — can create its own precondition.
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
- **Hosts a callback broker for host-level operations** that genuinely cannot run
  inside the container. On every container run the shim stands up a local JSON-RPC
  endpoint the container may call, and refuses anything not on an explicit verb
  list (see [Host-callback broker](#host-callback-broker)). This is the exception,
  not the default — containerized execution stays preferred for security.

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
mproj                      # report the workspace and docker reachability (cheap)
mproj doctor               # diagnose deeply, incl. a live "can a container start?" test
mproj init                 # bootstrap mono-config/, mono-repos-bare/ and mono-work/
mproj control [args]       # run mono-control (args forward to its CLI; use -- for flags)
mproj build-control        # build the mono-control image (mono-control:latest)
mproj shell-control        # interactive shell in the mono-control container
mproj test-control [args]  # run mono-control's tests (dev only; args forward to pytest)
```

### Bootstrapping the config repo

`mproj init` also settles what `mono-config/` should *contain*. A flag answers
outright; with no flag it asks, but only on a terminal:

```sh
mproj init --config-url URL   # clone an existing config repo
mproj init --config-fresh     # create a fresh, empty config repo (root commit, no remote)
mproj init --no-config        # just the empty directory — `init`'s behavior before this
mproj init                    # on a TTY: ask which of the three
```

Two properties worth relying on:

- **It stays idempotent.** An existing config repo short-circuits before any
  question, so re-running `init` on a working workspace never prompts. The
  interactive path only fires during a genuine bootstrap.
- **It never blocks.** With no flag and no terminal there is nobody to ask, so
  `init` fails naming the three flags rather than hanging on stdin — the same
  reflex as the non-interactive credential posture it clones under.

`init` refuses rather than guesses in the two ambiguous cases: a `mono-config/`
holding content but no `.git` is never clobbered, and a `--config-url` that
disagrees with an existing repo's `origin` is reported, not silently repointed.

### Health checks come in two layers

The layers differ in **what they prove**, and the wording is chosen to match:

| layer | runs | cost | catches |
| ----- | ---- | ---- | ------- |
| reachability (`mproj`, and before container runs) | `docker info`, bounded | sub-second | docker missing, daemon down |
| liveness (`mproj doctor`, only when asked) | actually starts a throwaway container, bounded | ~1–2s warm | a **wedged engine** |

The distinction is not academic. A Docker Desktop engine can answer `docker info`
perfectly while being unable to start a single container — it accepts the create
and never starts it, so every `mproj control` hangs forever. Reachability sees
nothing wrong. That is why `mproj` says *reachable*, never *available*, and points
at `doctor` in the same breath.

Reachability is a question about the **host alone**. It knows nothing about the
workspace, deliberately: an earlier version folded in "is there a `mono-control/`
checkout" and so reported `docker: unreachable` on an artifact-only workspace —
the ordinary way to *consume* the tool — while `mproj control` ran fine in it.

### Mode is reported, not judged

Both `mproj` and `mproj doctor` say which backend a workspace will use:

```
mode: dev - runs live source via Compose (mono-control/ checkout)
mode: artifact - runs the prebuilt mono-control:latest (no mono-control/ checkout)
```

Neither is a failure; the report mirrors `_dispatch`'s own test (the presence of a
`mono-control/` directory) so it cannot disagree with what actually runs. Note a
local `mono-control:latest` never beats a checkout, however fresh the image is.

A mode is only *not ready* when the selected backend genuinely cannot run: a
checkout whose `docker-compose.yml` is missing, or artifact mode with no image
built. Because mode is known, `doctor` also stops failing a dev workspace over a
missing `mono-control:latest` — dev mode never runs that image.

`mproj` is a report and always exits 0; `doctor` is the verdict and exits non-zero.

`doctor` tests with the already-built `mono-control:latest` and **never pulls**: a
doctor that reached the network would fail offline and prove less, since the
question is whether *this* engine can start a container it already has. No local
image is reported as its own finding, pointing at `mproj build-control`.

Container runs also carry a **startup watchdog**. If a run has not got going within
20s, the shim checks whether a container is sitting in `created` and, if so, prints
one line on stderr naming `mproj doctor`. It never cancels anything — a REPL
session or a long `test-control` is legitimately slow, so the watchdog's entire
power is that one hint.

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

### Host-callback broker

A few capabilities only the *host* has: a credential in an OS keyring, a native
filesystem, git itself. Today those are pushed **into** the container (the GitHub
token above). The broker is the inversion — the host keeps the capability and
exposes a **narrow verb** the container may ask it to perform. The container then
needs no token and no network, and host-only work stops crossing the bind-mount
seam, where a 9p/drvfs translation makes some of it outright impossible.

On every container run the shim binds a JSON-RPC 2.0 endpoint on an **ephemeral
port on `127.0.0.1`**, mints a **fresh per-run token**, and passes both in:

| variable            | value                     | how it is passed          |
| ------------------- | ------------------------- | ------------------------- |
| `MONO_BROKER_HOST`  | `host.docker.internal`    | `-e KEY=VALUE` (not secret) |
| `MONO_BROKER_PORT`  | the bound ephemeral port  | `-e KEY=VALUE` (not secret) |
| `MONO_BROKER_TOKEN` | per-run bearer token      | by name only, as the GitHub token is |

The token is the security gate: it is compared with `hmac.compare_digest`, and a
request that fails it gets a `401` with nothing else read or parsed. Requests are
audited to stderr (method, auth result, outcome) — never the token itself. The
audit logs at `WARNING`, so what you see unprompted is exactly the interesting
part: a failed authentication or a refused method. For the full per-request trail:

```python
logging.getLogger("mono_control_shim.broker").setLevel(logging.INFO)
```

**The verb list is the whole contract.** Anything not registered is refused with
JSON-RPC `-32601`. Today the list is deliberately trivial:

```
ping         -> "pong"
broker.info  -> {"version": ..., "methods": [...]}
```

The broker is **best-effort**: if it cannot bind, the shim warns and runs the
container without it. Nothing depends on it yet — semantic git/filesystem verbs,
and making it required, come later.

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
│   ├── test_cli.py         # stdlib unittest; token resolution + secret plumbing
│   ├── test_broker.py      # broker auth, refusal and JSON-RPC taxonomy
│   ├── test_init_config.py # `init`'s config source: clone / fresh / skip
│   └── test_verbs_git.py   # the git verb pack, against real git
└── mono_control_shim/
    ├── __init__.py
    ├── broker.py           # host-callback broker: transport + auth + verb dispatch
    ├── git_run.py          # shared git seam: subprocess, credential posture, auth
    ├── verbs/              # broker verb packs (git, mono_config)
    └── cli.py              # argparse CLI: workspace resolution + artifact ops
```

`git_run.py` sits below both callers on purpose. The CLI runs git *before* any
container exists (`init` populating `mono-config/`), so it cannot reach the verb
layer — but it must not therefore get a weaker credential posture. Anything that
knows about the managed model (slugs, bare roots, worktrees) stays in `verbs/git.py`;
only the seam itself is shared.
