import { useState } from "react";
import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SegmentedControl } from "./SegmentedControl";
it("selects with arrow keys, skips disabled options and keeps separate instances independent", async () => {
  function Test() { const [value, setValue] = useState("one"); return <><SegmentedControl label="First" value={value} onChange={setValue} options={[{ value: "one", label: "One" }, { value: "two", label: "Two", disabled: true }, { value: "three", label: "Three" }]} /><SegmentedControl label="Second" value="a" onChange={() => {}} options={[{value:"a",label:"A"},{value:"b",label:"B"}]} /></>; }
  const user = userEvent.setup(); render(<Test />);
  await user.tab(); await user.keyboard("{ArrowRight}");
  expect(screen.getByRole("button", { name: "Three" })).toHaveFocus();
  expect(screen.getByRole("button", { name: "Three" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "A" })).toHaveAttribute("aria-pressed", "true");
  await user.keyboard("{Home}"); expect(screen.getByRole("button", { name: "One" })).toHaveFocus();
  await user.tab(); expect(screen.getByRole("button", { name: "A" })).toHaveFocus();
});
