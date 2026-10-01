import clsx from "clsx";
import { useMemo, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { Skeleton } from "./Skeleton";

export type SortValue = string | number | boolean | null | undefined;

export interface Column<T> {
  id: string;
  header: ReactNode;
  cell: (row: T, index: number) => ReactNode;
  /** Enables sorting on this column. */
  sortValue?: (row: T) => SortValue;
  align?: "left" | "right" | "center";
  width?: number | string;
  className?: string;
  headerClassName?: string;
  /** Header tooltip. */
  title?: string;
  /** Allow cell content to wrap (default: single line). */
  wrap?: boolean;
}

export interface SortState {
  id: string;
  desc: boolean;
}

export interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T, event: MouseEvent<HTMLTableRowElement>) => void;
  dense?: boolean;
  /** Initial sort (uncontrolled). */
  initialSort?: SortState | null;
  /** Row selection with checkboxes. */
  selectable?: boolean;
  selected?: string[];
  onSelectionChange?: (keys: string[]) => void;
  loading?: boolean;
  /** Displayed when there are no rows. */
  empty?: ReactNode;
  /** Scroll container max height (sticky header). */
  maxHeight?: number | string;
  rowClassName?: (row: T) => string | undefined;
  className?: string;
  /** Hide the outer card border (when nested inside a Card). */
  bare?: boolean;
}

function compare(a: SortValue, b: SortValue): number {
  const an = a === null || a === undefined || (typeof a === "number" && Number.isNaN(a));
  const bn = b === null || b === undefined || (typeof b === "number" && Number.isNaN(b));
  if (an && bn) return 0;
  if (an) return 1; // nulls last
  if (bn) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b), "fr", { numeric: true, sensitivity: "base" });
}

/** Data table: sortable columns, clickable rows, optional multi-selection, dense mode. */
export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  dense,
  initialSort = null,
  selectable,
  selected = [],
  onSelectionChange,
  loading,
  empty,
  maxHeight,
  rowClassName,
  className,
  bare,
}: TableProps<T>) {
  const [sort, setSort] = useState<SortState | null>(initialSort);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.id === sort.id);
    if (!col?.sortValue) return rows;
    const get = col.sortValue;
    const out = [...rows].sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      const nullish = (v: SortValue) => v === null || v === undefined;
      if (nullish(va) !== nullish(vb)) return nullish(va) ? 1 : -1; // nulls always last
      const c = compare(va, vb);
      return sort.desc ? -c : c;
    });
    return out;
  }, [rows, sort, columns]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const allKeys = useMemo(() => sorted.map(rowKey), [sorted, rowKey]);
  const allSelected = allKeys.length > 0 && allKeys.every((k) => selectedSet.has(k));
  const someSelected = !allSelected && allKeys.some((k) => selectedSet.has(k));

  const toggleSort = (col: Column<T>) => {
    if (!col.sortValue) return;
    setSort((s) => (s?.id !== col.id ? { id: col.id, desc: true } : s.desc ? { id: col.id, desc: false } : null));
  };
  const toggleRow = (key: string) => {
    const next = new Set(selectedSet);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onSelectionChange?.([...next]);
  };
  const toggleAll = () => onSelectionChange?.(allSelected ? [] : allKeys);

  const cellPad = dense ? "px-3 py-1.5" : "px-4 py-2.5";
  const alignCls = (a?: string) => (a === "right" ? "text-right" : a === "center" ? "text-center" : "text-left");

  return (
    <div
      className={clsx("overflow-auto", !bare && "rounded-xl border border-border bg-surface shadow-sm", className)}
      style={{ maxHeight }}
    >
      <table className="w-full border-collapse text-[13px]">
        <thead className="sticky top-0 z-[1] bg-surface">
          <tr className="border-b border-border">
            {selectable && (
              <th className={clsx("w-8", dense ? "pl-3" : "pl-4")}>
                <input
                  type="checkbox"
                  aria-label="Tout sélectionner"
                  className="accent-[var(--accent)]"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someSelected;
                  }}
                  onChange={toggleAll}
                />
              </th>
            )}
            {columns.map((col) => {
              const active = sort?.id === col.id;
              return (
                <th
                  key={col.id}
                  title={col.title}
                  style={{ width: col.width }}
                  className={clsx(cellPad, "whitespace-nowrap text-[12px] font-medium text-muted", alignCls(col.align), col.headerClassName)}
                  aria-sort={active ? (sort!.desc ? "descending" : "ascending") : undefined}
                >
                  {col.sortValue ? (
                    // A real button: sortable headers are reachable and operable from the keyboard.
                    <button
                      type="button"
                      onClick={() => toggleSort(col)}
                      className={clsx("inline-flex select-none items-center gap-1 hover:text-fg", col.align === "right" && "flex-row-reverse")}
                    >
                      {col.header}
                      {active &&
                        (sort!.desc ? (
                          <ArrowDown size={12} strokeWidth={2} aria-hidden />
                        ) : (
                          <ArrowUp size={12} strokeWidth={2} aria-hidden />
                        ))}
                    </button>
                  ) : (
                    <span className={clsx("inline-flex items-center gap-1", col.align === "right" && "flex-row-reverse")}>{col.header}</span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {loading &&
            rows.length === 0 &&
            Array.from({ length: 5 }).map((_, i) => (
              <tr key={`sk-${i}`} className="border-t border-border first:border-t-0">
                {selectable && <td />}
                {columns.map((c) => (
                  <td key={c.id} className={cellPad}>
                    <Skeleton className="h-3.5 w-3/4" />
                  </td>
                ))}
              </tr>
            ))}
          {!loading && sorted.length === 0 && (
            <tr>
              <td colSpan={columns.length + (selectable ? 1 : 0)} className="px-4 py-10 text-center text-[13px] text-muted">
                {empty ?? "Aucune donnée"}
              </td>
            </tr>
          )}
          {sorted.map((row, i) => {
            const key = rowKey(row);
            const isSel = selectedSet.has(key);
            return (
              <tr
                key={key}
                onClick={onRowClick ? (e) => onRowClick(row, e) : undefined}
                // Clickable rows are focusable and open with Enter (Space is left to the checkbox).
                tabIndex={onRowClick ? 0 : undefined}
                onKeyDown={
                  onRowClick
                    ? (e: KeyboardEvent<HTMLTableRowElement>) => {
                        if (e.key === "Enter" && e.target === e.currentTarget) {
                          e.preventDefault();
                          onRowClick(row, e as unknown as MouseEvent<HTMLTableRowElement>);
                        }
                      }
                    : undefined
                }
                className={clsx(
                  "border-t border-border transition-colors first:border-t-0",
                  onRowClick && "cursor-pointer hover:bg-hover focus-visible:bg-hover focus-visible:outline-none",
                  isSel && "bg-accent-soft/50",
                  rowClassName?.(row),
                )}
              >
                {selectable && (
                  <td className={clsx("w-8", dense ? "pl-3" : "pl-4")} onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      aria-label="Sélectionner la ligne"
                      className="accent-[var(--accent)]"
                      checked={isSel}
                      onChange={() => toggleRow(key)}
                    />
                  </td>
                )}
                {columns.map((col) => (
                  <td key={col.id} className={clsx(cellPad, "align-middle", !col.wrap && "whitespace-nowrap", alignCls(col.align), col.className)}>
                    {col.cell(row, i)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
