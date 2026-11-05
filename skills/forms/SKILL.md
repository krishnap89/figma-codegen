---
name: forms
description: Forms with two or more inputs, validation errors and a submit action
when:
  kinds: [input]
  min_count: {input: 2}
priority: 60
---
- Inputs appear in spec order, one per row. Don't regroup them.
- Each input's text comes from its `state` property and changes through its `on<Binding>Change` callback.
- The submit button's `enabled` comes from state, e.g. `state.isSubmitEnabled`. Don't compute validation
  in the UI. If the required state shape has no such property, keep the spec's value and add
  `// TODO(state): enabled from state.isSubmitEnabled`.
- Error text is shown only when state carries it (`state.emailError != null`), through the input's own
  error parameter, not a separate `Text`. If the state has no error property, add
  `// TODO(state): <field> error`.
  TODO(uikit): the error parameter of our text field (`error`, `errorText`, `supportingText`?).
- Keyboard: every input except the last moves focus on IME action; the last one submits.
  - Compose: `ImeAction.Next` and then `ImeAction.Done` (calls the submit callback).
  - SwiftUI: `.submitLabel(.next)` and then `.submitLabel(.done)` with `.onSubmit { onLogin() }`.
  TODO(uikit): how our text field exposes keyboard options / IME action.
- Secure inputs (`isSecure: true` in the spec) use the UIKit field's secure mode, never a visual transformation
  written in the screen.

Do
- `enabled = state.isSubmitEnabled`
- `error = state.emailError`

Don't
- `enabled = state.email.isNotBlank() && state.password.length >= 8` (validation belongs in the ViewModel)
- Show errors in a separate `Text` under the field.
