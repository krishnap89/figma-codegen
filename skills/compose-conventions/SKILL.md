---
name: compose-conventions
description: How our Compose screens are structured, themed and previewed
platforms: [compose]
when:
  always: true
forbidden_patterns:
  - '\b(?:hiltViewModel|viewModel)\s*\('
  - '\bremember\s*\{\s*mutableStateOf'
priority: 100
---
The screen composable is stateless: it renders `state` and reports user actions through callbacks.

- State is hoisted. Read every value from `state`; never `remember { mutableStateOf(...) }` inside the screen.
- Callbacks are passed straight through: `onValueChange = onEmailChange`, `onClick = onLogin`.
  No lambdas that add logic.
- `modifier: Modifier = Modifier` is the last parameter. Apply it to the root layout only, first in the chain:
  `Column(modifier = modifier.fillMaxSize())`.
- Theme values come from tokens only: `AppTheme.colors.*`, `AppTheme.spacing.*`, `AppTheme.typography.*`.
  TODO(uikit): replace `AppTheme` with your theme accessor if it differs.
- The preview wraps the screen in the theme and passes a filled-in state:
  `AppTheme { LoginScreen(state = LoginScreenState(email = "a@b.c"), onEmailChange = {}, ...) }`.
  TODO(uikit): the name of your theme wrapper composable.

Do
- `Column(modifier = modifier.fillMaxSize().padding(horizontal = AppTheme.spacing.l))`
- One composable per screen file, plus the private preview.

Don't
- `viewModel()`, `hiltViewModel()`, `LaunchedEffect`, `collectAsState()` in the screen.
- Pass `Modifier` (capital M) to the root instead of the `modifier` parameter.
- Default parameter values for callbacks (`onLogin: () -> Unit = {}`).
