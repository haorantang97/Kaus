import type { MessageItem } from "./timeline/reducer";
import type { DictKey } from "../i18n";

// These failures occur before prompt/run submission. Transport errors and generic
// DriverError/send_failed do not establish whether the engine accepted the input.
// Evidence: SessionHost._require; session_router.send_message initialization and
// attachment checks; driver send_message busy/capability checks before prompt/run.
const REJECTION_REASON_KEYS: Readonly<Record<string, DictKey>> = {
  turn_already_running: "conversation.message.reason.running",
  unsupported_capability: "conversation.message.reason.unsupported",
  attachments_unsupported: "conversation.message.reason.attachments",
  runtime_not_active: "conversation.message.reason.inactive",
  runtime_start_failed: "conversation.message.reason.startFailed",
};

// Historical events retain their original diagnostics. Render the current copy
// from stable codes, without reclassifying unknown delivery outcomes as failures.
export function messageRejectionReasonKey(code: unknown): DictKey | null {
  return typeof code === "string" && Object.hasOwn(REJECTION_REASON_KEYS, code)
    ? REJECTION_REASON_KEYS[code]
    : null;
}

export function isConfirmedMessageRejection(item: Pick<MessageItem, "failureCode">): boolean {
  return messageRejectionReasonKey(item.failureCode) !== null;
}
