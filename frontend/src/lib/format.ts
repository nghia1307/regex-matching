/** Locale-aware thousands separators, used wherever a row or cell count is shown. */
export function formatNumber(value: number): string {
  return new Intl.NumberFormat().format(value);
}
