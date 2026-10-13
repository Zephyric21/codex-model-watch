const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'))
  .replace('load();setInterval(load,60000);', '');
const elements = new Map();
const context = vm.createContext({
  document: {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, {style: {}});
      return elements.get(selector);
    },
  },
  Chart: class {
    constructor(element, config) { element.chartConfig = config; }
    destroy() {}
  },
});
vm.runInContext(script, context);
const run = code => vm.runInContext(code, context);

if (process.env.TZ === 'Asia/Singapore') {
  assert.equal(run("localTime('2026-10-13T13:40:00Z')"), '10-13 21:40');
  assert.equal(run("localTime('2026-10-13T20:40:00Z',true,true)"), '2026-10-14 04:40:00');
  assert.equal(run("localTime('2026-10-13T13:00')"), '10-13 21:00');
  assert.equal(run("localTime('2026-10-13T13:40:00+00:00')"), '10-13 21:40');
  assert.equal(run("localTime('2026-10-13T21:40:00+08:00')"), '10-13 21:40');
  assert.equal(run("localTime(Date.parse('2026-10-13T13:40:00Z')/1000)"), '10-13 21:40');
  assert.equal(run("localTime(String(Date.parse('2026-10-13T13:40:00Z')/1000))"), '10-13 21:40');
  assert.equal(run("localTime(Date.parse('2026-10-13T13:40:00Z'))"), '10-13 21:40');
  assert.equal(run("localTime('')"), '—');
  assert.equal(run("localTime('invalid')"), '—');
  context.fixture = {
    summary: {}, coverage: {}, probe_summary: {},
    quota: {latest: {
      ts: '2026-10-13T13:40:00Z', primary_used: 20, secondary_used: 30,
      raw: JSON.stringify({primary: {reset_at: Date.parse('2026-10-13T13:40:00Z') / 1000}}),
    }},
    hourly: [{bucket: '2026-10-13T13:00', turns: 1, errors: 0}],
    errors_recent: [{ts: '2026-10-13T13:40:00Z', model: 'm', error_kind: 'error'}],
    probes: [{ts: '2026-10-13T13:40:00Z', requested: 'm', served: 'm'}],
  };
  run('render(fixture)');
  for (const selector of ['#tErr', '#tProbe', '#pSquares', '#quota']) {
    assert.match(elements.get(selector).innerHTML, /10-13 21:40/);
  }
  assert.equal(elements.get('#cTrend').chartConfig.data.labels[0], '10-13 21');
} else if (process.env.TZ === 'America/New_York') {
  assert.equal(run("localTime('2026-01-13T13:40:00Z')"), '01-13 08:40');
  assert.equal(run("localTime('2026-07-13T13:40:00Z')"), '07-13 09:40');
  assert.equal(run("localTime('2026-10-13T02:40:00Z',true)"), '2026-10-12 22:40');
} else {
  throw new Error('Run with TZ=Asia/Singapore or TZ=America/New_York');
}
console.log(`Local time checks passed (${process.env.TZ})`);
