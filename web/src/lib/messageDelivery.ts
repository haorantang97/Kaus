import type { MessageItem } from "./timeline/reducer";

// These failures occur before prompt/run submission. Transport errors and generic
// DriverError/send_failed do not establish whether the engine accepted the input.
// Evidence: SessionHost._require; session_router.send_message initialization and
// attachment checks; driver send_message busy/capability checks before prompt/run.
const CONFIRMED_REJECTIONS = new Set([
  "turn_already_running",
  "unsupported_capability",
  "attachments_unsupported",
  "runtime_not_active",
  "runtime_start_failed",
]);

export function isConfirmedMessageRejection(item: Pick<MessageItem, "failureCode">): boolean {
  return typeof item.failureCode === "string" && CONFIRMED_REJECTIONS.has(item.failureCode);
}
