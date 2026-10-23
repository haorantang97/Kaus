import { afterEach, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { ArtifactGallery } from "./ArtifactGallery";
import { fetchArtifact } from "../../lib/artifactApi";
vi.mock("../../lib/artifactApi", async original => ({ ...await original<typeof import("../../lib/artifactApi")>(), fetchArtifact: vi.fn() }));
const originalCreate = URL.createObjectURL; const originalRevoke = URL.revokeObjectURL;
afterEach(() => { vi.clearAllMocks(); URL.createObjectURL = originalCreate; URL.revokeObjectURL = originalRevoke; });
it("preloads only two neighbouring local images and never requests remote previews", async () => {
  URL.createObjectURL = vi.fn(() => "blob:neighbour"); URL.revokeObjectURL = vi.fn();
  vi.mocked(fetchArtifact).mockResolvedValue(new Blob(["image"], { type: "image/png" }));
  const sources = Array.from({ length: 7 }, (_, index) => ({ uri: `/image-${index}.png`, title: `Image ${index}` }));
  sources[4] = { uri: "https://remote.test/private.png", title: "Remote image" };
  const view = render(<ArtifactGallery sources={sources} index={3} conversationId="c1" onSelect={vi.fn()}><span>Current image</span></ArtifactGallery>);
  await waitFor(() => expect(fetchArtifact).toHaveBeenCalledTimes(3));
  expect(vi.mocked(fetchArtifact).mock.calls.map(call => call[1].uri)).toEqual(["/image-1.png", "/image-2.png", "/image-5.png"]);
  expect(view.container.querySelectorAll(".kaus-gallery-slide")).toHaveLength(5);
  await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(3));
  view.unmount(); expect(URL.revokeObjectURL).toHaveBeenCalledTimes(3);
});
