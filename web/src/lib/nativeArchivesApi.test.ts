import { beforeEach, expect, it, vi } from "vitest";
import { authorizedFetch } from "./authorizedFetch";
import { ArchiveApiError, nativeArchives } from "./nativeArchivesApi";

vi.mock("./authorizedFetch", () => ({ authorizedFetch: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

it("preserves the error code returned by the archive service", async () => {
  vi.mocked(authorizedFetch).mockResolvedValue(new Response(JSON.stringify({ error: { code: "archive_source_missing" } }), { status: 404 }));
  await expect(nativeArchives.detail("reference-1", "project-1")).rejects.toEqual(new ArchiveApiError("archive_source_missing"));
});

it("pins paged content and attachments to the opened revision", async () => {
  vi.mocked(authorizedFetch).mockImplementation(async () => new Response("{}"));
  await nativeArchives.detail("reference-1", "project-1", 100, "revision/1");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/reference-1?projectId=project-1&offset=100&limit=100&revision=revision%2F1", {});
  await nativeArchives.asset("reference-1", "project-1", "hash", undefined, "revision/1");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/reference-1/assets/hash?projectId=project-1&revision=revision%2F1", { signal: undefined });
});
