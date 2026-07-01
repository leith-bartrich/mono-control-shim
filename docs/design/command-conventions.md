# Command conventions

How `mproj` names its commands, and why. This is a convention to hold to as the
shim grows — especially as it learns to drive more than one artifact.

## The artifact model

`mproj` is a thin host shim ([mono_control_shim/cli.py](../../mono_control_shim/cli.py)).
Its job is to locate a workspace and hand off to **artifacts** — containerized
tools that run inside Docker, never on the host, for supply-chain isolation.

Today there is exactly one artifact: **mono-control**. But the command surface
is designed so that adding a second artifact (call it `foo`) introduces no new
shapes — only new names that slot into the existing pattern.

A command is one of three forms. The first two are about *using* an artifact;
the third is about *operating on* its container or image.

## Form 1 — `mproj <name>`: run the artifact

The bare artifact name is the **primary entrypoint**: it runs the artifact.

```
mproj control            # run mono-control
```

This is the hot path — the command typed most often — so it is deliberately
kept bare and alone in the top-level namespace. Nothing else is named `control`
or `control-…`, so `mproj cont<TAB>` completes cleanly to exactly one thing.
That tab-completion cleanliness is a design goal, not an accident (see
[Rationale](#rationale)).

## Form 2 — `mproj <name> <subcommand> …`: the artifact's own commands

Arguments after the artifact name are **forwarded into the artifact's own CLI**.
These are the artifact's user-facing subcommands; the shim does not interpret
them.

```
mproj control status         # -> mono-control status
mproj control validate
mproj control -- --version   # use -- to forward a flag
```

So an artifact's everyday, user-oriented surface lives *under* its name as
subcommands — the natural place to look for "things I do with this tool".

## Form 3 — `mproj <verb>-<name>`: operate on the artifact

Operations *on the artifact itself* — its image and container, not its CLI —
take the **`<verb>-<name>`** form:

```
build-control     # build the mono-control image (mono-control:latest)
shell-control     # open an interactive shell inside the container
test-control      # run mono-control's own test suite (dev only)
```

These are deliberately *not* subcommands of `control`, and deliberately *not*
bare top-level names. They are a distinct class — alternative, dev/ops, or
lifecycle capabilities — and the `<verb>-` prefix marks them as such. Reaching
for one is a deliberate act, which is appropriate: you don't `build-control` by
reflex the way you `control status`.

### Verb catalogue and mode applicability

The shim runs an artifact in one of two backends, named for the *thing they run*
rather than a vague environment:

- **dev mode** — a live source checkout, run via Docker Compose. Selected when a
  `mono-control/` checkout sits beside the workspace's `mono-config/`.
- **artifact mode** — the prebuilt, immutable image (`mono-control:latest`), run
  directly. Selected when there is no checkout. (We avoid "prod" / "image" /
  "container" here: every mode uses images and containers, and the real
  distinction is live source vs. a *built artifact*.)

The backend is chosen automatically by checkout presence, but `--artifact` on the
relevant verbs forces artifact mode even when a checkout exists — e.g. to run the
shipped image as a user would, from a dev tree. Not every verb applies in both:

| verb            | dev | artifact | notes                                         |
| --------------- | --- | -------- | --------------------------------------------- |
| *(run)* Form 1  | ✓   | ✓        | live source in dev; baked image in artifact   |
| `build-<name>`  | ✓   | —        | builds the image *from* a checkout            |
| `shell-<name>`  | ✓   | ✓        | a shell in whichever container the mode runs  |
| `test-<name>`   | ✓   | —        | needs source + dev deps; the artifact has neither |

`--artifact` is offered only where it makes sense: on run and `shell-<name>`. It
is *not* on `test-<name>` — there is nothing to test inside a built artifact (no
source, no test deps), so testing is inherently dev-only. A verb that cannot apply
in a mode fails fast with a clear message rather than degrading silently.

## Rationale

- **Tab-completion of the hot path.** The most-used command, `mproj <name>`,
  must complete with no competing siblings. If lifecycle operations were named
  `control-build`, `control-test`, … they would crowd the `control` completion
  space. Putting the verb *first* (`build-control`) keeps the `<name>` namespace
  clean and groups the verbs by what they do at the front.
- **A real conceptual split.** Forms 1–2 are *using the artifact* (run it, call
  its subcommands) — the user-facing surface. Form 3 is *operating on the
  artifact's container/image* — the dev/ops surface. Different audiences,
  different lifecycles; the naming makes the boundary visible.
- **Deliberateness as a feature.** Form 3 entries are less guessable and less
  reflexive on purpose. They are the things you reach for intentionally, so a
  more deliberate entry point is correct, not a cost.

## Not every command is an artifact operation

Some `mproj` commands act on the **workspace**, not on any artifact, and so do
not take the `<verb>-<name>` form:

```
mproj            # default: report the workspace and container availability
mproj init       # bootstrap mono-config/, mono-repos/ and mono-repos-offline/ dirs
```

These are shim-native — they are about the workspace itself. Keep them as plain
verbs; the `<verb>-<name>` form is reserved for operating on a specific artifact.

## Generalizing to a second artifact

The whole point of the convention is that a new artifact needs no new shape.
Suppose the shim later drives a `foo` artifact. It inherits the pattern wholesale:

```
mproj foo               # run foo
mproj foo <subcommand>  # foo's own CLI
build-foo               # build foo's image
shell-foo               # shell into foo's container
test-foo                # run foo's tests
```

No new command *kinds* — only new *names* in the established slots. That is the
property the convention exists to preserve.
