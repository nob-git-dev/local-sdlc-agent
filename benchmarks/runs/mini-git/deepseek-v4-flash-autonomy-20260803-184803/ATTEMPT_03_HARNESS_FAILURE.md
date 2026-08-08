# Mini Git Attempt 03: Reasoning-only Completion Failure

- Frozen harness commit: `e4b7cf3`, with generalized Attempt 01/02 corrections under evaluation
- Product progress: S01 approved and copied back; S02 generated and repaired only inside its isolated worktree
- Recorded child API calls: 7; observed physical calls: 8 (the failed final Judge call was not counted by the old manifest)
- Parent recorded API calls: 11; observed physical calls: 12
- External mid-run intervention: no
- Result: S02 failed after its third Judge call

## Observed Failure

S02's 20 agent-authored tests passed after three Coder rounds. The independent
Judge had already found two defects not covered by those tests, and the Coder
patched them. On the final review, DeepSeek emitted about 8,000 reasoning
chunks but no `content`; the response budget ended before a verdict document
was produced. The runner classified the empty mandatory conclusion as a
configuration error and stopped.

The same run also demonstrated a vacuous-evidence risk: a `unittest discover`
command can report zero discovered tests before a stage creates its test file.
An executable evidence gate must not treat that as proof merely because a
runtime returns zero.

## Generalized Correction

1. Model reasoning-only output as a completion-protocol state, not a product
   failure.
2. For a thinking-enabled analysis function, authorize exactly one independent
   retry with the same role, system prompt, and input documents, but with
   thinking off, temperature zero, and at most 4,096 output tokens.
3. Charge the retry to the Action Gate and API budget and record both physical
   requests plus a structured `completion_recoveries` entry.
4. Never retry a no-thinking call recursively; a second empty conclusion fails
   closed.
5. Reject recognized test-runner evidence that executed zero tests, regardless
   of runtime-specific exit-code behavior.
