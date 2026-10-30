const test = require('node:test');
const assert = require('node:assert/strict');
const data = require('../site_tools/public_data.js');

test('missing, whitespace and nonfinite values stay missing; zero survives', () => {
  for (const value of [null, undefined, '', ' ', NaN, Infinity, true]) assert.equal(data.finite(value), null);
  assert.equal(data.finite(0), 0);
  assert.equal(data.finite('0'), 0);
  assert.equal(data.format(0.05, '%'), '<1%');
});
test('chart gaps do not become zero or a line across missing hours', () => {
  const rows = [{hour:'06:00',rain_probability:0}, {hour:'07:00',rain_probability:null},
    {hour:'08:00',rain_probability:100}, {hour:'11:00',rain_probability:50}];
  const chart = data.chart(rows, 'rain', 320, 200);
  assert.equal(chart.points[1].val, null);
  assert.deepEqual(chart.segments.map(s=>s.length), [1,1,1]);
  assert.equal(chart.points[0].val, 0);
  assert.equal(chart.high, 100);
});
test('charts fit each requested viewport without clipping or invented values', () => {
  for (const width of [320,360,375,390,430,768,1024,1440,1920]) {
    const rows = Array.from({length:24}, (_,h)=>({hour:String(h).padStart(2,'0')+':00',temp_c:20+h/2}));
    const chart = data.chart(rows, 'temp', width, 200);
    assert.equal(chart.points.length, 24);
    assert.ok(chart.points.every(p=>p.x>=0 && p.x<=width && p.y>=0 && p.y<=200));
    assert.equal(chart.points[23].x, width-chart.padX);
  }
});
test('wind chart uses its own range, including exactly zero', () => {
  const chart = data.chart([{hour:'12:00',wind_kmh:0},{hour:'13:00',wind_kmh:12}], 'wind', 900, 200);
  assert.equal(chart.high, 15);
  assert.equal(chart.points[0].val, 0);
});
test('timeline includes hours present only at another location', () => {
  const feature=(slug,hour)=>({properties:{slug,date_iso:'2026-10-30',hour}});
  assert.deepEqual(data.timeline([feature('dago','09:00'),feature('other','10:00'),feature('dago','09:00')]),
    [{date_iso:'2026-10-30',hour:'09:00'},{date_iso:'2026-10-30',hour:'10:00'}]);
});
test('wind flow arrow uses meteorological from direction and hides calm', () => {
  assert.equal(data.windRotation(0,10),90);
  assert.equal(data.windRotation(90,10),180);
  assert.equal(data.windRotation(360,10),90);
  assert.equal(data.windRotation(120,0),null);
  assert.equal(data.windRotation(null,10),null);
});
