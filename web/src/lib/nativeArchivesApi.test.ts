import { beforeEach, expect, it, vi } from "vitest";
import { authorizedFetch } from "./authorizedFetch";
import { ArchiveApiError, nativeArchives } from "./nativeArchivesApi";

vi.mock("./authorizedFetch", () => ({ authorizedFetch: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

it("defaults preview requests to native references and sends explicit archive selection", async () => {
  vi.mocked(authorizedFetch).mockImplementation(async () => new Response(JSON.stringify({ previewToken: "p" })));
  await nativeArchives.preview(["s1"], "project-1");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/preview", expect.objectContaining({
    method: "POST", body: JSON.stringify({ sourceIds: ["s1"], projectId: "project-1", storage: "reference" }),
  }));
  await nativeArchives.preview(["s1"], "project-1", "archive");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/preview", expect.objectContaining({
    body: JSON.stringify({ sourceIds: ["s1"], projectId: "project-1", storage: "archive" }),
  }));
});

it("creates an independent snapshot with the selected Project scope", async () => {
  vi.mocked(authorizedFetch).mockResolvedValue(new Response(JSON.stringify({ archiveIds: ["snapshot-1"], count: 1 })));
  await expect(nativeArchives.archive("reference/1", "project-1")).resolves.toEqual({ archiveIds: ["snapshot-1"], count: 1 });
  expect(authorizedFetch).toHaveBeenCalledWith("/api/native-archives/reference%2F1/archive", expect.objectContaining({
    method: "POST", body: JSON.stringify({ projectId: "project-1" }),
  }));
});

it("preserves the archive-required error returned for a reference backup", async () => {
  vi.mocked(authorizedFetch).mockResolvedValue(new Response(JSON.stringify({ error: { code: "archive_reference_requires_archive" } }), { status: 409 }));
  await expect(nativeArchives.backup(["reference-1"], "project-1")).rejects.toEqual(new ArchiveApiError("archive_reference_requires_archive"));
});

it("pins paged content and attachments to the opened revision", async () => {
  vi.mocked(authorizedFetch).mockImplementation(async () => new Response("{}"));
  await nativeArchives.detail("reference-1", "project-1", 100, "revision/1");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/reference-1?projectId=project-1&offset=100&limit=100&revision=revision%2F1", {});
  await nativeArchives.records("reference-1", "project-1", 10, "revision/1");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/reference-1/records?projectId=project-1&offset=10&limit=10&revision=revision%2F1", {});
  await nativeArchives.asset("reference-1", "project-1", "hash", undefined, "revision/1");
  expect(authorizedFetch).toHaveBeenLastCalledWith("/api/native-archives/reference-1/assets/hash?projectId=project-1&revision=revision%2F1", { signal: undefined });
});
