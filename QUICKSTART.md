# First screen in 15 minutes

Generate a Compose screen from the bundled sample frame first, then from a frame of your own. This guide
skips the compile sandbox, your own UIKit and few-shot examples. Add those later; see
[What next](#what-next).

You need:

- Python 3.9+
- the URL of an OpenAI-compatible LLM endpoint (the team's Qwen server, for example)
- a Figma personal access token, for step 4 only (read-only scope is enough)

## 1. Install and configure (3 min)

```bash
pip install -e .
cp codegen.example.yaml codegen.yaml
```

In `codegen.yaml`, set the two `llm` values for your server:

```yaml
llm:
  base_url: http://qwen.internal:8000/v1   # your endpoint
  model: qwen-coder                        # the name the server serves the model under
```

The example config also points `platforms.compose` at a sandbox project (`../codegen-sandbox-android`) that
you don't have yet. Turn compiling off by commenting out `sandbox_file` and `compile_cmd` under
`platforms.compose`. Otherwise `generate` stops with `compile_cwd does not exist`.

Check the result:

```bash
figma-codegen config     # effective settings, secrets masked
```

## 2. See what the model will see (2 min, offline)

The repo has a saved Figma response for a login screen. It needs no network and no token:

```bash
figma-codegen spec --figma-json tests/fixtures/login_nodes.json
```

This prints the simplified spec, which is the only description of the design the model gets. Look for:

- **tokens instead of raw values**: `"gap": "spacing.m"`, `"color": "color.text.primary"`
- **UIKit components** from `manifest/components.json`: `"component": "TextField"` with its props
- **warnings** on stderr. This frame has two on purpose: a hand-drawn graphic, and `PromoBanner`, a
  component that isn't in the manifest.

## 3. Generate the sample screen (2–5 min, depending on the model)

```bash
figma-codegen generate --figma-json tests/fixtures/login_nodes.json \
  --platform compose --out out/LoginScreen.kt --package com.example.login
```

Progress shows each round. On success you get `✓ out/LoginScreen.kt` and the note `not compiled — configure a
sandbox`. That's expected for now: the code passed the design-system lint only.

- The code uses the **sample** UIKit (`com.company.uikit.*`, `AppTheme.spacing.m`), so it won't compile in
  your app until you set up your own manifest and tokens.
- Exit code `1` means the code still failed the lint after all rounds. The last attempt is saved as
  `out/LoginScreen.kt.draft`, with the errors printed.
- Every prompt, tool call and lint result is logged under `.codegen/traces/`.

## 4. Your own frame (5 min)

In Figma, select a frame and use **Copy link to selection**. The link must contain `node-id=`.

```bash
export FIGMA_TOKEN=figd_...
figma-codegen fetch "<frame-link>"       # prints the saved file: .codegen/figma/<fileKey>_<nodeId>.json
figma-codegen spec --figma-json .codegen/figma/<fileKey>_<nodeId>.json
figma-codegen generate --figma-json .codegen/figma/<fileKey>_<nodeId>.json \
  --platform compose --out out/MyScreen.kt --package com.example.my
```

`spec` and `generate` also take the link directly. Reusing the saved file saves Figma API requests and
lets you rerun offline.

Expect plenty of warnings on a real frame for now. The sample manifest knows only six components (Button,
TextField, Link, Check Box, Top Navigation Bar, Fixed Footer Action). Anything else is drawn as plain layout,
and colors only match the sample tokens.

## When something goes wrong

| symptom | fix |
|---|---|
| `compile_cwd does not exist` | comment out `sandbox_file` / `compile_cmd` (step 1) |
| `LLM request failed … Check llm.base_url / llm.model` (exit 3) | wrong endpoint or model name; `curl <base_url>/models` lists what the server serves |
| errors about tools or `tool_choice` | the server has no tool calling: add `--no-tools`, or for vLLM start it with `--enable-auto-tool-choice --tool-call-parser …` |
| `missing manifest/components.json` | run from the repo root, or set `paths.manifest` / `paths.tokens` |
| `FIGMA_TOKEN is not set` | `export FIGMA_TOKEN=figd_...` in the same shell |
| `403 Forbidden` from Figma | the token's account can't open that file |
| `URL has no node-id` | use **Copy link to selection** on the frame, not the file URL |

## What next

The steps are in the order they improve output. The [README](README.md) covers each one:

1. **Your design system.** Draft the manifest and tokens from your Compose UIKit with
   `figma-codegen import-compose` ([README §0](README.md#0-existing-compose-uikit-draft-both-files-from-the-code)),
   or write them by hand ([§1](README.md#1-fill-in-the-manifest-manifestcomponentsjson),
   [§2](README.md#2-tokens-manifesttokensjson)).
2. **A compile sandbox.** Compiler errors are what let the model fix its own mistakes
   ([§3](README.md#3-compile-sandbox-strongly-recommended)).
3. **Two real screens as few-shot examples** ([examples/README.md](examples/README.md)).
4. **The visual check.** `generate --compare --open` puts the Figma export next to the rendered code
   ([Visual check](README.md#visual-check-no-ai)).
