# Few-shot examples

The single biggest quality lever for a mid-size model. Add 2–3 real screens your team wrote:

```
examples/
  compose/
    ProfileScreen.spec.json   # output of: figma-codegen spec <frame-url> --out ...
    ProfileScreen.kt          # the code your team actually shipped for that frame
  swiftui/
    ProfileScreen.spec.json
    ProfileScreen.swift
```

Pick screens that show your conventions (forms, lists, empty states). Keep each file short;
`agent.max_examples` (default 2) limits how many go into each prompt.
