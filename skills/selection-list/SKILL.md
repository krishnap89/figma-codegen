---
name: selection-list
description: Lists of options where the user picks one (radio) or several (checkboxes)
when:
  repeated_rows: true
  components: [Check Box]
  min_count: {Check Box: 3}
priority: 55
---
TODO(uikit): list every component with `stateType: Boolean` under `when.components` (checkbox, radio,
switch) with `min_count` 3.

- Decide single or multi choice from the control: radio buttons mean single choice, checkboxes or
  switches mean multi choice. When unsure, use multi choice and add `// TODO(design): single or multi?`.
- State holds one selection value, not one Boolean per row: `selectedOption: String?` (single) or
  `selectedOptions: Set<String>` (multi). Rows are derived from a list of options.
  If the required state shape lists one property per row, keep that shape (it wins) and add
  `// TODO(state): replace per-row flags with one selected value`.
- The whole row is the touch target, not only the control:
  - Compose: `Modifier.fillMaxWidth().selectable(selected, onClick, role = Role.RadioButton)` for single
    choice, `.toggleable(value, onValueChange = ..., role = Role.Checkbox)` for multi choice. The control
    inside gets `onCheckedChange = null` so the click isn't handled twice.
  - SwiftUI: `.contentShape(Rectangle())` on the row, then `.onTapGesture`.
  TODO(uikit): whether our checkbox / radio accepts a null callback.
- Rows with the same structure are built in one loop over the options, not copied once per row.

Do
- `options.forEach { option -> Row(Modifier.selectable(...)) { ... } }`

Don't
- `val option1Checked: Boolean, val option2Checked: Boolean, ...` when you are free to choose the state.
- Make only the checkbox clickable.
