# ZCode ACP bridge

The bridge runs the user's `zcode app-server --stdio` and translates the published
ZCode Protocol v1 compatibility interface to the common Kaus ACP driver. It does
not copy or read the user's credentials, modify global model defaults, import
other conversations, or auto-answer permission and user-input requests.

Source contract: `zai-org/ZCode`, `packages/shared/src/zcode-protocol/index.ts`,
`zcode-protocol-legacy-types.ts`, and the CLI bootstrap protocol server. The
upstream repository is Apache-2.0; this adapter is an original implementation.
The protocol currently coexists with V4 and is marked for eventual removal.
An unsupported future version must fail visibly, never fall back to a stateless
prompt or a new session during resume.

Contract:

- Each normal or Group conversation receives its own native process, storage and
  session database using the official `ZCODE_STORAGE_DIR` and
  `ZCODE_SESSION_DB_PATH` options. Provider credentials remain owned by the native
  application. The bridge's private manifest maps its ID to the native ID;
  resume reuses the same database, native ID and workspace. No imported history
  is sent. This also contains native permission-mode writes to Project defaults.
- Models and thought levels come from native session settings. Provider/model
  pairs are opaque JSON IDs so identical names remain distinct. Changes set
  `persistAsWorkspaceLastUsed=false`.
- Native build/edit/yolo/plan are approval modes. The unimplemented native auto
  mode is not offered. Only native options selected by the user are returned.
- Project MCP is passed in session create/resume with `isolation=session`.
- Native turns remain pending until a matching terminal event; cancellation,
  errors, transport closure, and stale or foreign events must not report success.
- Text, thought, tool updates, files, images and usage are translated from the
  native event/snapshot interfaces. Native child-session attribution is retained
  in ACP metadata; a full child-task graph also needs the common UI to consume it.
  Text-only persisted tool snapshots cannot replace richer terminal image output.
- Questions become ACP elicitation and permissions become ACP permission RPCs.
  Decline/cancel and malformed answers fail closed. Repeated native interaction
  requests share the same user decision rather than opening duplicate dialogs.
- Runtime preferences disable native automatic question answering and native
  memory injection; Kaus provides its own explicit conversation/Group context.
- ZCode desktop-specific browser execution or account header callbacks have no
  portable Kaus provider. They fail explicitly; no credential or authorization
  is fabricated. Personal API providers configured in the native CLI use their
  own native configuration.

Verification must cover the contract independently of having a logged-in model.
Native cloud output, images and tool execution require a configured user engine
and remain separate from contract simulation or a no-login startup probe.

The built native 0.16.9 runtime and the packaged relative-path launcher passed
11-step runs against a local model fixture: three isolated sessions, mode
isolation, streaming, cold resume and a resumed follow-up. See the stage's
`sources-zcode.md` and `native/zcode/contract-packaged-fixture.json` for evidence.
No real cloud provider or permission approval was used. The current native
runtime does not persist an empty conversation before its first input; loading
such an empty draft fails visibly rather than creating a replacement session.
