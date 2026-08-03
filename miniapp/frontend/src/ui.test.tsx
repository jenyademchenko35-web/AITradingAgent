import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { EmptyState, FilterBar, MetricCard, SearchBar } from "./ui";

test("reusable cards expose tooltip, loading and accessible empty-state semantics", () => {
  const { rerender } = render(<MetricCard label="PF" value={1.4} tooltip="Published profit factor" />);
  expect(screen.getByTitle("Published profit factor")).toHaveTextContent("1.4");
  rerender(<MetricCard label="PF" value={null} loading />);
  expect(screen.getByLabelText("Загрузка")).toBeInTheDocument();
  render(<EmptyState title="No data" detail="Not published" />);
  expect(screen.getByRole("status")).toHaveTextContent("Mini App displays published data only.");
});

test("filter chips and search controls are keyboard-accessible", () => {
  const onFilter = vi.fn(); const onSort = vi.fn(); const onChange = vi.fn(); const onClear = vi.fn();
  render(<><FilterBar filter="WATCH" sort="confidence" onFilter={onFilter} onSort={onSort} /><SearchBar value="BTC" onChange={onChange} onClear={onClear} /></>);
  expect(screen.getByRole("button", { name: "WATCH" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Очистить поиск" }));
  expect(onClear).toHaveBeenCalledOnce();
  fireEvent.keyDown(screen.getByLabelText("Поиск символа"), { key: "Escape" });
  expect(onClear).toHaveBeenCalledTimes(2);
});
