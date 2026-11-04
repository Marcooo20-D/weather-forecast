const { test } = require("node:test");
const assert = require("node:assert/strict");
const field = require("../site_tools/weather_field.js");
const grid = () => ({
  schema_version: 1,
  status: "ready",
  model: { id: "gfs013" },
  grid: { latitudes: [-7, -6], longitudes: [107, 108], nx: 2, ny: 2 },
  times: ["2026-11-04T00:00:00+07:00"],
  fields: {
    u: [[0, 2, 4, 6]],
    v: [[-5, -5, -5, -5]],
    temperature: [[20, 22, 24, 26]],
    humidity: [[70, 80, 90, 100]],
    cloud: [[0, 0, 0, 0]],
    precipitation: [[0, 0, 0, 0]],
  },
});
test("bilinear interpolation follows native coordinates", () => {
  assert.equal(
    field.sample(grid(), 0, -6.5, 107.5, ["temperature"]).temperature,
    23,
  );
  assert.equal(field.sample(grid(), 0, -6.5, 107.5).u, 3);
});
test("wind crossing north does not flip south", () => {
  const d = grid();
  d.fields.u = [[1, -1, 1, -1]];
  const p = field.sample(d, 0, -6.5, 107.5);
  assert.equal(p.u, 0);
  assert.equal(p.v, -5);
  assert.equal(field.direction(p.u, p.v), 0);
});
test("no extrapolation beyond native coverage", () => {
  assert.equal(field.sample(grid(), 0, -5.9, 107.5), null);
  assert.equal(field.sample(grid(), 0, -6.5, 108.1), null);
});
test("missing neighbours stay missing; exact valid nodes survive", () => {
  const d = grid();
  d.fields.temperature[0][3] = null;
  assert.equal(field.sample(d, 0, -6.5, 107.5, ["temperature"]), null);
  assert.equal(field.sample(d, 0, -7, 107, ["temperature"]).temperature, 20);
});
test("rain is hourly amount; real zero is preserved", () => {
  assert.equal(field.valueAt(grid(), 0, -6.5, 107.5, "rain"), 0);
  assert.equal(
    field.valueAt(grid(), 0, -6.5, 107.5, "wind"),
    Math.hypot(3, -5) * 3.6,
  );
});
test("malformed data cannot activate layers", () => {
  const d = grid();
  d.fields.u[0][0] = NaN;
  assert.throws(() => field.validate(d));
  assert.throws(() => field.validate({ status: "unavailable" }));
});
