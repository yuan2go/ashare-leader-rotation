// Drive the frozen browser functions directly from the raw fixture.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ctx = vm.createContext({input, URL});
const source = ['engine', 'input'].map(n =>
  fs.readFileSync(path.join(__dirname, '..', 'offline', n + '.js'), 'utf8')).join('\n');
vm.runInContext(source, ctx);
function execute() {
  const d = prepare(input.raw, input.end, true);
  if (input.checkInput) return d.errors;
  if (d.errors.length) throw Error(d.errors.join('\n'));
  if (input.signals) return replay(d, input.signals, input.mode, input.span);
  const signals = scan(d);
  // This fixture has one defense candidate and one trend slot. Use the frozen
  // single-slot executors; transfer the unused sleeve budgets back to total cash.
  const months = new Map();
  for (const day of d.week_ends) months.set(day.slice(0, 7), day);
  const horizon = new Date(d.days.at(-1) + 'T00:00:00Z');
  horizon.setUTCDate(horizon.getUTCDate() + 1);
  const monthsClosed = new Set([...months.values()].filter(day =>
    day.slice(0, 7) < horizon.toISOString().slice(0, 7)));
  const defense = [], trend = [];
  let reserved = [];
  for (let i = 0; i < d.days.length; i++) {
    const day = d.days[i], prior = d.days[i-1];
    if (monthsClosed.has(day)) reserved = Object.keys(input.basic[prior] || {});
    defense.push([...reserved]);
    trend.push(signals.ranks[i].filter(code => !reserved.includes(code)));
  }
  const slot = (capital, ranks) => replay({...d, policy:{...d.policy, capital}},
    {...signals, ranks}, 'R1', input.span, 0, input.delay, input.slip);
  const value = slot(3333.33, defense), momentum = slot(10000, trend);
  return {ranks: signals.ranks, weekEnds: d.week_ends, months: [...monthsClosed],
    orders: [...value.orders.map(o => [o.date, 'ValueDiv', o.code, o.shares, o.price]),
             ...momentum.orders.map(o => [o.date, 'Trend', o.code, o.shares, o.price])],
    path: value.path.map((v,i) => {
      const t = momentum.path[i], unallocated = 666667;
      return [v.date, v.hold, t.hold, unallocated + v.cash + t.cash, unallocated + v.equity + t.equity];
    })};
}
process.stdout.write(JSON.stringify(vm.runInContext('(' + execute.toString() + ')()', ctx)));
