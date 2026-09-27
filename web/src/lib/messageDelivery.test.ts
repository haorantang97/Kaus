import { describe, expect, it } from "vitest";
import { isConfirmedMessageRejection } from "./messageDelivery";

describe("confirmed message rejection", () => {
  it.each(["turn_already_running", "unsupported_capability", "attachments_unsupported", "runtime_not_active", "runtime_start_failed"])("allows retry for the documented pre-submission code %s", (failureCode) => {
    expect(isConfirmedMessageRejection({ failureCode })).toBe(true);
  });
  it.each([undefined, null, "", "send_failed", "message_rejected", "timeout", "http_error", "gateway_unreachable", "network_error"])("does not infer rejection from %s", (failureCode) => {
    expect(isConfirmedMessageRejection({ failureCode })).toBe(false);
  });
});
