# figma-codegen

CLI agent that turns a Figma frame into Jetpack Compose or SwiftUI code built with **your UIKit
and design tokens**, using your **self-hosted Qwen** through its OpenAI-compatible API.
No external AI services; the only outbound call is to the Figma REST API.

```
figma-codegen generate "https://www.figma.com/design/AbC123/App?node-id=12-300" \
  --platform compose --out feature/login/LoginScreen.kt --package com.company.feature.login
```

## How it works

```
Figma frame ──(REST API)──► simplify.py ──► compact spec JSON (+ warnings)
                              │ uses manifest/components.json + manifest/tokens.json
                              ▼
                         prompts.py: spec + docs of used components + token table + few-shot examples
                              ▼
          ┌──────────── round (max agent.max_rounds) ─────────────┐
          │ Qwen ── may call read-only tools ──► final code block  │
          │   get_component_docs, list_tokens                       │
          │   search_code, read_file  (only with --repo)            │
          │ harness: design-system lint + compile in sandbox        │
          │   fail → errors back to Qwen, history compacted         │
          └─────────────────────────────────────────────────────────┘
                              ▼
               LoginScreen.kt   (or LoginScreen.kt.draft + errors on failure)
```

Design decisions, chosen for a mid-size self-hosted model:

- **Deterministic everywhere except code writing.** Figma parsing, token resolution, linting and
  compiling are plain code. The model only writes the file.
- **Few, read-only tools.** The harness writes files and runs the compiler; the model can only look
  things up. Use `--no-tools` if your server has no tool calling.
- **Token table in the prompt.** The model gets `spacing.l -> AppTheme.spacing.l` mappings
  instead of guessing names.
- **History compaction.** Each fix round sees only the task, the latest code and the latest errors.
- **Traces.** Every request, tool call and validation is logged as JSONL in `.codegen/traces/`.

## Setup

```bash
pip install -e .            # Python 3.10+; deps: openai, httpx, pyyaml
cp codegen.example.yaml codegen.yaml
export FIGMA_TOKEN=figd_...  # Figma personal access token (read-only scope is enough)
```

Edit `codegen.yaml`: `llm.base_url` and `llm.model` for your Qwen server. For vLLM, tool calling
needs `--enable-auto-tool-choice --tool-call-parser <parser for your Qwen version>`.

### Configuring from outside (env vars, flags, shared config)

Every setting can be supplied without touching the project's `codegen.yaml`. Precedence, lowest → highest:

1. built-in defaults
2. config file — `--config PATH`, else `$CODEGEN_CONFIG`, else `./codegen.yaml`,
   else `~/.config/figma-codegen/codegen.yaml` (respects `$XDG_CONFIG_HOME`)
3. environment variables
4. CLI: `--set section.key=value` (repeatable) and the shortcut flags `--base-url`, `--api-key`,
   `--figma-token`, `--model`

| env var | config key |
|---|---|
| `CODEGEN_LLM_BASE_URL` | `llm.base_url` |
| `CODEGEN_LLM_MODEL` | `llm.model` |
| `CODEGEN_LLM_API_KEY` | `llm.api_key` |
| `CODEGEN_LLM_TEMPERATURE` | `llm.temperature` |
| `CODEGEN_LLM_MAX_TOKENS` | `llm.max_tokens` |
| `CODEGEN_MAX_ROUNDS` | `agent.max_rounds` |
| `CODEGEN_MAX_TOOL_CALLS` | `agent.max_tool_calls_per_round` |
| `CODEGEN_USE_TOOLS` | `agent.use_tools` (`true`/`false`) |
| `CODEGEN_MAX_EXAMPLES` | `agent.max_examples` |
| `FIGMA_TOKEN` / `CODEGEN_FIGMA_TOKEN` | `figma.token` |
| `CODEGEN_MANIFEST`, `CODEGEN_TOKENS`, `CODEGEN_EXAMPLES_DIR`, `CODEGEN_TRACE_DIR`, `CODEGEN_REPO_ROOT` | `paths.*` |

`--set` reaches any key, including per-platform ones; values are parsed as YAML so numbers,
booleans and lists work:

```bash
figma-codegen --base-url http://gpu-box:8000/v1 \
  --set llm.max_tokens=8192 \
  --set platforms.compose.compile_cmd='["./gradlew", ":app:compileDebugKotlin"]' \
  generate <frame-url> --platform compose --out LoginScreen.kt

figma-codegen config            # print the effective config (secrets masked) and active env vars
```

Relative paths from the config file resolve against that file's directory; relative paths from env
vars or `--set` resolve against the current directory. Prefer env vars over flags for secrets so
they don't land in shell history.

### 1. Fill in the manifest (`manifest/components.json`)

One entry per UIKit component. The included Button / TextField / Link entries show the format:

| field | meaning |
|---|---|
| `figma` | component (set) name in the Figma library |
| `figmaKey` | component set key; get it with `figma-codegen components <library-file-url>` |
| `kind` | `input` → creates a state binding, `action` → creates a callback |
| `props.*.figmaProp` | Figma variant/boolean property; `map` translates values |
| `props.*.figmaText` | read text from a named text layer inside the instance (hidden layers ignored) |
| `compose` / `swiftui` | import, signature and one usage example |

`tokenCode`, `categoryCode` and `primitives` describe how tokens and layout primitives are written
in your code. Adjust them to your theme API (e.g. `AppTheme.spacing.m`).

### 2. Tokens (`manifest/tokens.json`)

W3C design-token JSON, exported from Figma Variables or Tokens Studio. Replace the sample file
with your export, ideally from CI so it stays in sync.

### 3. Compile sandbox (strongly recommended)

A tiny project that depends on your UIKit. The generated file is written into it and compiled
each round; compiler errors are what let the model fix its own mistakes.

- **Android:** a Gradle module that depends on UIKit; `compile_cmd: ["./gradlew", ":app:compileDebugKotlin", "-q"]`.
  Keep the Gradle daemon warm.
- **iOS:** an Xcode project or SwiftPM package that depends on UIKit; build with `xcodebuild` for an
  iOS Simulator destination. This needs macOS.

Without a sandbox, only the lint runs and the CLI says so.

### 4. Few-shot examples

See `examples/README.md`. Two real screens from your codebase usually help more than any prompt tweak.

## Commands

```bash
figma-codegen spec <frame-url>                       # inspect what the model will see + warnings
figma-codegen generate <frame-url> --platform compose --out LoginScreen.kt --package com.x.login
figma-codegen generate <frame-url> --platform swiftui --out LoginScreen.swift --repo ../ios-app
figma-codegen lint LoginScreen.kt --platform compose
figma-codegen components <library-file-url>          # list library components + keys (manifest work)
```

Useful flags for `generate`: `--strict` (abort on Figma warnings), `--no-tools`, `--model`,
`--repo` (enables code search tools), `--figma-json` (offline: saved `/v1/files/:key/nodes` response).

Exit codes: `0` success, `1` code still failing (`.draft` written), `2` config/design problem,
`3` LLM endpoint error.

## Figma rules for designers

The simplifier reports problems instead of guessing. Output quality depends on:

- auto-layout on every frame,
- only UIKit component instances (no detached components or hand-drawn shapes),
- colors, spacing and text styles bound to variables or styles that match tokens,
- meaningful layer names (`EmailField`, `LoginButton`): they become `state.email` and `onLogin`.

Run `figma-codegen spec` on a frame to see its warnings.

## Customising

| want to change | file |
|---|---|
| lint rules (raw dp, banned imports…) | `codegen/validate.py` → `DEFAULT_RULES`, or `forbidden_patterns` in config |
| generated screen shape (state, callbacks, preview) | `codegen/prompts.py` → `PLATFORMS[...]["shape"]` |
| system rules | `codegen/prompts.py` → `SYSTEM` |
| Figma → spec mapping | `codegen/simplify.py` |
| add a tool | `codegen/tools.py` (method + schema) |

## Tests

```bash
pip install -e '.[dev]' && pytest
```

Tests use a saved Figma response (`tests/fixtures/login_nodes.json`) and a scripted fake model,
so they need no network, Figma token or LLM.

## Next steps

- **Evaluation set:** 10–20 frames with known-good code; rerun after every prompt, manifest or model change.
- **CI mode:** run `generate` from a pipeline triggered by a Jira ticket with a Figma link, and open a PR.
- **MCP server:** the same `Tools`, `simplify` and `validate` functions can be exposed through the `mcp` SDK for IDE clients.
- **Figma Variables API (Enterprise):** resolve bound variable IDs to token names directly instead of matching by value.
