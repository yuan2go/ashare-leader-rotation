type OptionalNumber = number | null | undefined;

const absent = "—";
const numberFormat = new Map<string, Intl.NumberFormat>();

function format(value: OptionalNumber, options: Intl.NumberFormatOptions, key: string) {
  if (value == null || !Number.isFinite(value)) return absent;
  let formatter = numberFormat.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat("zh-CN", options);
    numberFormat.set(key, formatter);
  }
  return formatter.format(value);
}

export const integer = (value: OptionalNumber) => format(value, {maximumFractionDigits: 0}, "integer");
export const quantity = (value: OptionalNumber) => format(value, {maximumFractionDigits: 6}, "quantity");
export const decimal = (value: OptionalNumber, digits = 2) =>
  format(value, {minimumFractionDigits: digits, maximumFractionDigits: digits}, `dec-${digits}`);
export const rmb = (value: OptionalNumber, digits = 2) => format(value, {
  style: "currency", currency: "CNY", currencyDisplay: "narrowSymbol",
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}, `rmb-${digits}`);
export const cents = (value: OptionalNumber, digits = 2) => rmb(value == null ? null : value / 100, digits);

function unit(value: OptionalNumber, suffix: string, signed: boolean) {
  const result = format(value, {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: signed ? "exceptZero" : "auto",
  }, `unit-${signed}`);
  return result === absent ? absent : result + suffix;
}

export const pct = (value: OptionalNumber, signed = false) => unit(value, "%", signed);
export const pp = (value: OptionalNumber, signed = false) => unit(value, " pp", signed);
export const text = (value: string | null | undefined) => value || absent;
export const changeClass = (value: OptionalNumber) => value == null || value === 0 ? ""
  : value > 0 ? "market-up" : "market-down";

export function timestamp(value: string | null | undefined) {
  return value ? value.replace("T", " ").replace(/\.\d+(?=[+-Z]|$)/, "") : absent;
}
