# Command Code bridge

This original Kaus bridge exposes the documented Command Code headless interface
through the same ACP driver used by ordinary conversations and Group agents.
It does not copy, bundle, or replace the native Agent implementation.

The native executable is `command-code`. The `cli.py` absolute entrypoint works
from any conversation working directory; `--command` and `--state-dir` allow
isolated test or managed runtime installations. Login remains the user's native
`command-code login` flow.

## Contract

- Starts each turn with `-p --output-format json --no-auto-update
  --skip-onboarding --permission-mode plan`; the prompt is sent on stdin.
- Native `--list-models` supplies the model list. Engine default omits `--model`.
  A model or effort selection is passed with `--model` / `--effort`.
- Default plan mode retains the native plan-file exception. Full access maps to
  `yolo` only after an explicit permission selection. The headless interface has
  no documented approval or question-response RPC, so ask/auto-edit are absent.
- Text, thought, and tool lifecycle NDJSON events become standard ACP updates.
  A missing/error result is an error, never successful completion.
- An ACP UUID maps to one exact native session ID. Resume uses `--resume <id>`;
  no latest-session lookup, continuation prompt, or history summarization occurs.
  Load replays the bridge's own saved transcript without calling a model.
- Cancellation terminates the native process group and preserves the known
  native session ID. A turn timeout also terminates the process.
- MCP projection and binary attachments are explicitly unsupported by this
  headless contract. Existing native MCP/configuration remains CLI-owned.

Bridge records are private UUID files under Kaus's state directory. Cross-cwd
restore is rejected. Native credentials and configuration are neither read by
the bridge nor rewritten; no HOME override is used.

## Evidence

Official interface references, reviewed 2026-09-27:
[headless](https://commandcode.ai/docs/headless),
[CLI reference](https://commandcode.ai/docs/reference/cli),
[permissions](https://commandcode.ai/docs/permissions),
[settings](https://commandcode.ai/docs/settings).

The isolated native 1.66.0 installation supplied 82 conversational models and
successfully created a bridge session. The test prompt returned authentication
required, so native inference and native tool behavior remain unverified on this
machine. Protocol subprocess tests additionally verify default denial, explicit
bypass, model/effort selection, streaming, exact-ID recovery, cancellation,
timeouts, and the shared Kaus driver path. These are adapter tests, not a claim
that the vendor model was exercised.

The npm package declares `UNLICENSED`. Kaus uses only the documented local
process interface; distribution of the upstream implementation requires its
own permission.
