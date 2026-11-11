# figma-codegen

CLI agent that turns a Figma frame into Jetpack Compose or SwiftUI code built with **your UIKit
and design tokens**, using your **self-hosted Qwen** through its OpenAI-compatible API.
No external AI services; the only outbound call is to the Figma REST API.

```
figma-codegen generate "https://www.figma.com/design/AbC123/App?node-id=12-300" \
  --platform compose --out feature/login/LoginScreen.kt --package com.company.feature.login
```

New here? [QUICKSTART.md](QUICKSTART.md) gets you from install to a generated screen on a sample frame in
about 15 minutes.

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
pip install -e .            # Python 3.9+; deps: openai, httpx, pyyaml
cp codegen.example.yaml codegen.yaml
export FIGMA_TOKEN=figd_...  # Figma personal access token (read-only scope is enough)
```

Edit `codegen.yaml`: `llm.base_url` and `llm.model` for your Qwen server. For vLLM, tool calling
needs `--enable-auto-tool-choice --tool-call-parser <parser for your Qwen version>`.

Replies are streamed by default (`llm.stream`), which avoids 504s from a gateway in front of the server.

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
| `CODEGEN_LLM_STREAM` | `llm.stream` (`true`/`false`) |
| `CODEGEN_MAX_ROUNDS` | `agent.max_rounds` |
| `CODEGEN_MAX_TOOL_CALLS` | `agent.max_tool_calls_per_round` |
| `CODEGEN_USE_TOOLS` | `agent.use_tools` (`true`/`false`) |
| `CODEGEN_MAX_EXAMPLES` | `agent.max_examples` |
| `CODEGEN_MAX_SKILL_CHARS` | `agent.max_skill_chars` |
| `CODEGEN_SKILL_TOOL` | `agent.skill_tool` (`true`/`false`) |
| `FIGMA_TOKEN` / `CODEGEN_FIGMA_TOKEN` | `figma.token` |
| `CODEGEN_MANIFEST`, `CODEGEN_TOKENS`, `CODEGEN_EXAMPLES_DIR`, `CODEGEN_SKILLS_DIR`, `CODEGEN_TRACE_DIR`, `CODEGEN_FIGMA_JSON_DIR`, `CODEGEN_COMPARE_DIR`, `CODEGEN_REPO_ROOT` | `paths.*` |

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

### 0. Existing Compose UIKit? Draft both files from the code

```bash
figma-codegen components <library-file-url> > figma-components.json      # optional: exact Figma names/keys
figma-codegen import-compose ../android-app/uikit --app ../android-app \
  --figma-components figma-components.json                               # -> manifest/imported/
```

- **Tokens** come from the Kotlin theme, so token paths are names the code can reference. The importer reads
  `Color(0xFF…)`, `Color.White`, `.copy(alpha = …)`, `N.dp`, `TextStyle(…)` and `.copy(…)`, and follows
  references between vals. It finds them in top-level vals, objects (`object Dimens { val spacingM = 16.dp }`),
  theme classes reached through an object getter or a CompositionLocal (`AppTheme.colors.textPrimary`), and
  Material builders (`lightColors(…)`, `Typography(…)`). Only the light theme is used. Dp values are split into
  spacing / radius / border / size by name.
- When `tokenCode` can't produce a token's Kotlin expression, the token stores it in
  `$extensions.figma-codegen.compose`. Top-level palette vals like `Gray900` are one example.
- **Components** come from public `@Composable` functions. Previews, `*Theme` and private/internal functions
  are skipped. Each component gets its signature (with overloads) and KDoc. `kind` and `stateType` are read
  from `onXChange` / `onX` params. `props` are drafted from enum and text params. With `--app`, the example is
  the shortest real call site; without it, the example is synthesized.
- `import-report.md` lists everything that needs a human: guessed Figma names, values the importer couldn't
  evaluate, missing KDoc, and Figma components with no composable. It sorts components by call sites so you
  can start with the ones that matter.

Review the drafts, then point `paths.manifest` / `paths.tokens` at them (or copy them over) and run
`figma-codegen spec <frame>` on a few real screens.

### 1. Fill in the manifest (`manifest/components.json`)

One entry per UIKit component. The included Button / TextField / Link entries show the format:

| field | meaning |
|---|---|
| `figma` | component (set) name in the Figma library |
| `figmaKey` | component set key; get it with `figma-codegen components <library-file-url>` |
| `kind` | `input` → creates a state binding, `action` → creates a callback |
| `stateType` | type of an `input` binding: `String` (default), `Boolean` or `Int` → e.g. `val agree: Boolean = false` / `var agree: Bool = false` |
| `props.*.figmaProp` | Figma component property; `map` translates values. Use the short name (`Style`, `Button Text`) or the exact key (`Text#905:0`) when a variant and a text property share a name; variants win the short name. INSTANCE_SWAP values become the swapped component's name (`arrow-right`) |
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

## Skills

A skill is a small folder of team conventions that goes into the prompt **only when the screen needs it**:
form rules for forms, list rules for lists. Prompts stay short, and teams can add conventions without
editing `codegen/prompts.py`. Three starter skills are in `skills/`: `compose-conventions`, `forms` and
`selection-list`. Fill in their `TODO(uikit)` lines with your real APIs.

```
skills/forms/
  SKILL.md                  # front matter + instructions (required)
  examples/                 # optional: <Name>.kt / <Name>.swift, each optionally with <Name>.spec.json
```

```markdown
---
name: forms                      # must equal the folder name
description: Forms with text inputs, validation and a submit action
platforms: [compose, swiftui]    # default: both
when:                            # no `when` = only with --skill (or load_skill)
  always: false                  # true = every run on these platforms
  components: [TextField]        # any of these manifest components in the spec...
  min_count: {TextField: 2}      # ...at least this often (keys from `components` or `kinds`; default 1)
  kinds: [input]                 # any component with this manifest `kind`
  repeated_rows: false           # a container with 3+ child containers of the same structure
forbidden_patterns: []           # extra lint regexes, active only when this skill is in the prompt
priority: 50                     # higher goes first, and is dropped last (default 50)
---
- Short rules, one per line.
```

**Selection** is deterministic and uses only the simplified spec, never the model. A skill is picked if
its platform matches and `always` is true or **any** `when` condition holds. The match says why, and
`generate` prints it:

```
skills: compose-conventions (always), forms (kinds: input ×2)
```

`repeated_rows` compares the structure of sibling containers: types and components, recursively, ignoring
texts and names. Four option rows with a checkbox and a label match; two rows, or rows with different
children, don't.

**In the prompt**, picked skills appear in priority order under `## Team conventions for this screen`,
just before `## Task`. With no skills, the prompt is exactly what it was before skills existed. Skill
examples count toward `agent.max_examples` before the global `examples/`, which fill any remaining slots.
A picked skill's `forbidden_patterns` join the lint for that run, reported as
`forbidden by skill 'forms': <pattern>`.

**Budget:** `agent.max_skill_chars` (default 6000) caps the skill text (bodies + examples) per prompt.
Over budget, the lowest-priority skills are dropped first and the CLI names them. Skills forced with
`--skill` are never dropped.

**Model-chosen skills** (`agent.skill_tool: true`, off by default): the system prompt also lists the skills
that weren't picked (name + description), and the model can call `load_skill(name)`. Loaded text counts
against the same budget, and its lint patterns apply too.

```bash
figma-codegen skills                                  # list skills + validation; exit 1 if any is invalid (CI)
figma-codegen spec <frame-url>                        # also shows which skills generate would pick, and why
figma-codegen generate <frame-url> ... --skill forms  # force a skill (repeatable)
figma-codegen generate <frame-url> ... --no-skills    # no automatic selection (--skill still applies)
```

The trace's `start` event records the skills, their reasons, the dropped skills and the total skill
characters. `load_skill` calls are traced like other tools. Invalid skills (missing `SKILL.md`, bad YAML,
a name that doesn't match the folder, an unknown `when` key, a broken regex) are skipped with a warning.
They never stop a run.

### Writing a good skill

- **Short rules, not prose.** A mid-size model follows ten bullet points better than three paragraphs.
  Keep `SKILL.md` under about 40 lines and end with a short *Do / Don't* list.
- **One example at most**, and a short one. Examples take few-shot slots and budget.
- **Don't fight the harness.** The required screen shape (state properties, callbacks) is fixed by
  `prompts.py`. When a convention needs something the shape doesn't have, tell the model to add a
  `// TODO(state): ...` instead of changing the shape.
- **Narrow `when` conditions.** A skill that is always picked is part of the system prompt in all but name.
- **Enforce what matters** with `forbidden_patterns`, so the fix loop catches violations.
- **Measure.** Run the evaluation set (see Next steps) with and without the skill (`--no-skills`,
  `--skill`). Keep it only if the output gets better.

## Commands

```bash
figma-codegen fetch <frame-url>                      # only download + save the Figma JSON
figma-codegen spec <frame-url>                       # inspect what the model will see + warnings
figma-codegen generate <frame-url> --platform compose --out LoginScreen.kt --package com.x.login
figma-codegen generate <frame-url> --platform swiftui --out LoginScreen.swift --repo ../ios-app
figma-codegen lint LoginScreen.kt --platform compose
figma-codegen components <library-file-url>          # list library components + keys (manifest work)
figma-codegen skills                                 # list skills and check they are valid
figma-codegen compare <frame-url> --code LoginScreen.kt --open   # Figma export vs. rendered code
figma-codegen status src/                            # generated screens: which frame, edited since?
```

Useful flags for `generate`: `--strict` (abort on Figma warnings), `--no-tools`, `--no-stream`, `--model`,
`--skill NAME` / `--no-skills` (see [Skills](#skills)),
`--repo` (enables code search tools), `--no-baseline` (see [Baseline](#baseline)), `--figma-json` (offline: saved `/v1/files/:key/nodes` response),
`-v/--verbose` (also on `spec`: list every Figma warning).

While it runs, progress goes to stderr: each step (fetching Figma, simplifying, `round 2/4 · generating`,
`round 2/4 · lint + compile`) shows its elapsed time and the total. On a terminal the running step is a
live spinner line that says what it is waiting on (the model, or a tool call). In CI logs, steps that
take more than 2s print a start line and a "still running" line every 30s. Output is colored on a
terminal only; `NO_COLOR=1` turns color off, `FORCE_COLOR=1` turns it on for logs that render ANSI.
Figma warnings are grouped by type with counts and example layers:

```
Figma: 20 warnings — fixing these in Figma gives better code
  8  hand-drawn graphics                  Frame 17, Overlay ×5, Frame 7 +1 more
  4  colors not bound to a color style    Frame 9, Frame 10, Frame 11 +1 more
  run with --verbose to list every warning
```

Every download (`fetch`, `spec`, `generate`) saves the raw Figma response to
`.codegen/figma/<fileKey>_<nodeId>.json`, so you can re-run offline with `--figma-json <that file>`
without spending API requests. Change the folder with `paths.figma_json` (empty disables saving), or
one file with `--save-figma-json PATH`.

Exit codes: `0` success, `1` code still failing (`.draft` written), `2` config/design problem,
`3` LLM endpoint error. `status`: `0` all listed files have a usable baseline, `1` a file has no
baseline or a corrupt one, `2` the path does not exist.

## Baseline

A successful `generate` remembers where the screen came from, so a later design change can be
diffed against it and applied as a targeted update instead of a full regeneration (planned
`diff` / `update` commands). Next to the output it writes `<OutFile>.codegen.json`, e.g.
`LoginScreen.kt.codegen.json`, containing:

| field | |
|---|---|
| `figma` | `file_key`, `node_id`, `url`, `last_modified` (all `null` when `--figma-json` was used without a URL) |
| `platform`, `screen`, `package` | as passed to `generate` |
| `spec` | the full simplified spec the code was generated from |
| `code_sha256` | hash of the written file, so you can tell when someone has edited it |
| `manifest_sha256`, `tokens_sha256` | hashes of the design-system files used |
| `tool_version`, `generated_at` (UTC), `model` | |

The generated file starts with a two-line comment pointing at the frame and the baseline
(before `package`; plain `//` comments, so lint and compile are unaffected):

```kotlin
// Generated by figma-codegen from Figma AbC123 node 12:300
// Baseline: LoginScreen.kt.codegen.json — visual layer; update with `figma-codegen update`
package com.company.login
```

Commit the baseline with the code: it doubles as the index from Figma frame to code file.
A failed run (`.draft`) writes no baseline; `--no-baseline` skips both the file and the header.

`figma-codegen status <file-or-dir>` lists every generated screen with its frame, when it was
generated and whether the code has been modified since:

```
src/login/LoginScreen.kt  modified since generation
  LoginScreen (compose) · Figma AbC123 node 12:300 · generated 2026-11-11T10:02:41Z with qwen-coder
```

## Visual check (no AI)

Lint and compile prove the code is valid, not that it looks like the design. The visual check puts the
Figma image export next to a screenshot of the generated screen; a person compares them.

```bash
figma-codegen generate <frame-url> --platform compose --out LoginScreen.kt --compare --open
figma-codegen compare <frame-url> --code LoginScreen.kt --open          # any time later
figma-codegen compare <frame-url> --screenshot preview.png              # no sandbox: your own capture
```

It writes `.codegen/compare/<Screen>/index.html`, one offline file with both images embedded (attach it to
the PR), plus `figma.png` and `render.png`. The page has four views: **side by side**, **onion skin**
(code over Figma, opacity slider), **swipe**, and **difference** (identical pixels are black, so shifted
spacing shows as bright bands). Keys `1`–`4` switch views. Both images are drawn at the frame's width
in dp/pt, so an @2x Figma export and an xxhdpi/@3x screenshot line up. If their heights differ, the
page and CLI say so.

- **Figma image:** `/v1/images` export at `--scale` (default 2), cached as
  `.codegen/figma/<fileKey>_<nodeId>@2x.png`; with `--figma-json` the cached image is reused, so
  offline reruns work. `--figma-image PNG` skips the API.
- **Screenshot:** `--screenshot PNG` uses any image you have (Android Studio preview, Xcode canvas,
  emulator). Otherwise the sandbox renders it (below).
- `generate --compare` checks the render setup before spending model rounds, and only runs after a
  valid result. A failed visual check is reported but doesn't change the exit code.

### Rendering in the sandbox

A test can't call the generated preview (`private @Preview`, or the `#Preview` macro). So the harness
writes one more file into the sandbox, `CodegenRenderTarget` (next to `sandbox_file`, or at
`render_entry_file`). It builds the screen with default state and no-op callbacks:

```kotlin
@Composable
fun CodegenRenderTarget() {
    LoginScreen(state = LoginScreenState(), onEmailChange = {}, onPasswordChange = {}, onForgotPassword = {}, onLogin = {})
}
```

Your screenshot test renders `CodegenRenderTarget()` and never changes. While a screen is generated,
the file holds an empty stub, so the sandbox always compiles. `render_cmd` runs in `render_cwd`
(default `compile_cwd`) and gets the frame size and target path as env vars, also usable as
`{out}` `{width}` `{height}` `{name}` placeholders in the command:

| env var | value |
|---|---|
| `CODEGEN_RENDER_OUT` | absolute path to write the PNG to (or set `render_output`, a path/glob the test writes) |
| `CODEGEN_RENDER_WIDTH` / `_HEIGHT` | Figma frame size in dp/pt (`0` if unknown) |

The same vars are also set with a `TEST_RUNNER_` prefix, which `xcodebuild test` passes into the test process.

**Compose: [Roborazzi](https://github.com/takahirom/roborazzi) on Robolectric (JVM, no emulator).** Add
the `io.github.takahirom.roborazzi` plugin and `roborazzi-compose`, `robolectric`, `ui-test-junit4`
test dependencies to the sandbox app, with `unitTests.isIncludeAndroidResources = true`. Then:

```kotlin
// app/src/test/java/sandbox/RenderTest.kt
@RunWith(AndroidJUnit4::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class RenderTest {
    @Test fun render() {
        fun env(k: String) = System.getenv(k)?.toFloatOrNull()?.toInt()?.takeIf { it > 0 }
        RuntimeEnvironment.setQualifiers("w${env("CODEGEN_RENDER_WIDTH") ?: 360}dp-h${env("CODEGEN_RENDER_HEIGHT") ?: 800}dp-xxhdpi")
        captureRoboImage(System.getenv("CODEGEN_RENDER_OUT") ?: "build/codegen-render.png") {
            AppTheme { CodegenRenderTarget() }
        }
    }
}
```

```yaml
render_cmd: ["./gradlew", ":app:testDebugUnitTest", "--tests", "sandbox.RenderTest", "--rerun", "-q", "--offline"]
```

`--rerun` is needed because Gradle doesn't count env vars as test inputs and would skip an "up-to-date"
test. Paparazzi works too: point `render_output` at its snapshot folder (a glob such as
`app/src/test/snapshots/images/*.png`, newest file wins).

**SwiftUI: an XCTest in a test target hosted by the sandbox app** (`UIHostingController` renders
text fields and scroll views that `ImageRenderer` can't):

```swift
@MainActor final class RenderTests: XCTestCase {
    func testRender() throws {
        let env = ProcessInfo.processInfo.environment
        func dim(_ k: String, _ d: Double) -> Double { Double(env[k] ?? "").flatMap { $0 > 0 ? $0 : nil } ?? d }
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: dim("CODEGEN_RENDER_WIDTH", 390),
                                            height: dim("CODEGEN_RENDER_HEIGHT", 844)))
        window.rootViewController = UIHostingController(rootView: CodegenRenderTarget())  // wrap in your theme if needed
        window.makeKeyAndVisible()
        window.layoutIfNeeded()
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        try image.pngData()!.write(to: URL(fileURLWithPath: env["CODEGEN_RENDER_OUT"] ?? NSTemporaryDirectory() + "render.png"))
    }
}
```

```yaml
render_cmd: ["xcodebuild", "test", "-scheme", "Sandbox", "-destination", "platform=iOS Simulator,name=iPhone 16",
             "-only-testing:SandboxTests/RenderTests/testRender", "-quiet"]
```

Add `CodegenRenderTarget.swift` to the Xcode target once (any content; the harness overwrites it).
The SwiftUI entry passes the event closures in the order the prompt lists them, which is the order the
model is told to declare them. If the model reorders them, the render fails to compile with a clear error.

## What the simplifier does with real files

- **Library components not in the manifest** (e.g. "Top Navigation Bar", "Main Content") are expanded
  as plain layout, so their content isn't lost; the node keeps `"figmaComponent"` and a warning says it
  was expanded. Only an empty unknown instance becomes `"Unmapped"`.
- **Hand-drawn artwork** (vectors, shapes, and frames/groups/instances containing only those) becomes one
  `{"type": "Graphic", "name", "sizePx"}` node with one warning, instead of one warning per vector.
  Instances mapped in the manifest are never treated as artwork.
- **Wrapper frames** that hold a single child and draw nothing (no background, radius, border or padding)
  are replaced by their child, which inherits `fill` / `weight`.
- **Colors** match a token by the bound Figma style's name first (`Carbon / Darkest` →
  `color.carbon.darkest`; case and punctuation ignored), then by hex value. Text styles match the same way
  before falling back to font size/weight. Unmatched colors say whether a style was bound.
- **Borders**: a visible solid stroke becomes `"border": {"width", "color"}`; a fully transparent stroke
  becomes `"color": "transparent"` (space reserved for a selected state) without a warning. Widths use
  `border.*` tokens if that category exists, else `"2px"`.
- **`gap`** is emitted only for containers with two or more children.

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
| team conventions for some screens (forms, lists…) | `skills/<name>/SKILL.md` (see [Skills](#skills)) |
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
