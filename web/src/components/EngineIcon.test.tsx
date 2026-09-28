import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { EngineIcon } from "./EngineIcon";
import { ENGINE_BRANDS, engineBrand } from "../lib/backend-display";

describe("engine identity", () => {
  it("resolves backend ids through the same identity registry", () => {
    for (const [key, brand] of Object.entries(ENGINE_BRANDS)) {
      expect(engineBrand(`backend:${key}`)).toBe(brand);
    }
  });
  it("renders real marks and uses a graphical fallback for custom engines", () => {
    const { container } = render(<><EngineIcon engine="backend:hermes" /><EngineIcon engine="Claude Code" /><EngineIcon engine="unlisted" /></>);
    expect([...container.querySelectorAll("[data-engine-icon]")].map(item => item.getAttribute("data-engine-icon"))).toEqual(["hermesagent", "claudecode", "generic"]);
    expect(container.textContent).toBe("");
  });
});
