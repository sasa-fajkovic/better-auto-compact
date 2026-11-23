---
name: compact-handoff
description: Write or refresh the session handoff file so work resumes after context compaction without losing anything. Use when a hook or the user asks for a handoff, or before compacting.
---

1. Write to the path the hook message gave you. Without one, use `.agent-handoff/HANDOFF.md` at the repository root. If the file exists, read it, then replace its whole content with your file-writing tool and keep it under 8 KB. It is a snapshot: never append to it, not even with shell `>>`, `tee -a` or `echo`, and never use it as a running log; keep progress logs in a separate file. The hook asks for a rewrite when it grows past 8 KB. Do not edit `floor.md` next to it; the hook rewrites it at every compaction.
2. If the project has a task tracker file, update it first so it matches reality.
3. Copy exact values, never paraphrase them: branch names, commit SHAs, build or artifact ids, issue numbers, URLs and ports, error and rule codes, measured numbers, versions, PIDs, paths. Include values that appeared only in tool output. When a value was replaced, keep both as `old -> new (current)`.
4. Use exactly these sections:

```
# Handoff (<UTC timestamp>)
## 0. Resume prompt      one imperative paragraph telling the next agent what to do first
## 1. State              done / not done
## 2. Next action        the single next step
## 3. Decisions          each decision, the rejected alternatives, and why
## 4. User rules         every instruction or constraint the user gave, quoted verbatim
## 5. Working set        files touched and what changed
## 6. In flight          background jobs, half-finished edits
## 7. Verification       what was tested, result, at which commit
## 8. Open questions     questions to raise with the user, and when
## 9. Facts              every exact value from tool output or the user that is not in the repo, with what it is
```

5. Then continue the task you were doing. Do not end your reply with a note about the handoff.
