/* Real Chromium release checks. Run against a generated, bounded public site.
 * npm install playwright@1.62.1 leaflet@1.9.4 && npx playwright install --with-deps chromium
 * node tests/browser_smoke.cjs _site browser-evidence
 * Public input snapshots test the UI; these tests do not establish forecast skill.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');

const site = path.resolve(process.argv[2] || '_site');
const artifacts = path.resolve(process.argv[3] || 'browser-evidence');
const report = {browser: 'chromium', source: 'committed public input snapshots',
  externalAssets: 'Real Leaflet 1.9.4 from pinned npm package; fonts and basemap network intentionally blocked',
  viewports: [], checks: [], pageErrors: [], localFailures: [], externalFailures: []};
let browser, currentPage, currentContext;
const mime = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.geojson': 'application/geo+json',
  '.csv': 'text/csv', '.txt': 'text/plain; charset=utf-8'};
const server = http.createServer(async (req, res) => {
  try {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    const target = path.resolve(site, '.' + (pathname.endsWith('/') ? pathname + 'index.html' : pathname));
    if (!target.startsWith(site + path.sep)) { res.writeHead(403).end(); return; }
    const body = await fs.readFile(target);
    res.writeHead(200, {'Content-Type': mime[path.extname(target)] || 'application/octet-stream'}).end(body);
  } catch { res.writeHead(404).end('Not found'); }
});

async function main() {
  await fs.mkdir(artifacts, {recursive: true});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const manifest = JSON.parse(await fs.readFile(path.join(site, 'langit_portal_manifest.json'), 'utf8'));
  assert.equal(manifest.locations.length, 3, 'all forecast locations are present');
  report.version = manifest.version;
  report.sourceRevision = manifest.source_revision;
  browser = await chromium.launch({headless: true});
  report.browserVersion = browser.version();

  for (const viewport of [{width: 1440, height: 900}, {width: 390, height: 844}]) {
    report.viewports.push(viewport);
    const context = await browser.newContext({viewport, timezoneId: 'Asia/Jakarta', reducedMotion: 'reduce'});
    currentContext = context;
    // Keep application interactions independent of font/tile service availability.
    await context.route('https://unpkg.com/leaflet@1.9.4/dist/*', async route => {
      const name = new URL(route.request().url()).pathname.split('/').pop();
      if (!['leaflet.js', 'leaflet.css'].includes(name)) { await route.abort(); return; }
      await route.fulfill({body: await fs.readFile(require.resolve('leaflet/dist/' + name)),
        contentType: name.endsWith('.js') ? 'text/javascript' : 'text/css'});
    });
    await context.route(/https:\/\/(?:fonts\.(?:googleapis|gstatic)\.com|[^/]*basemaps\.cartocdn\.com)\//,
      route => route.abort());
    await context.tracing.start({screenshots: true, snapshots: true});
    const page = await context.newPage();
    currentPage = page;
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => report.pageErrors.push({url: page.url(), message: error.message}));
    page.on('response', response => {
      if (response.url().startsWith(base) && response.status() >= 400)
        report.localFailures.push({url: response.url(), status: response.status()});
    });
    page.on('requestfailed', request => {
      const entry = {url: request.url(), error: request.failure()?.errorText};
      if (entry.url.startsWith(base)) {
        if (entry.error !== 'net::ERR_ABORTED') report.localFailures.push(entry);
      } else report.externalFailures.push(entry);
    });
    async function go(route) {
      const response = await page.goto(base + '/' + route, {waitUntil: 'domcontentloaded', timeout: 30000});
      console.log(`${viewport.width}: ${route}`);
      assert.equal(response.status(), 200, route);
      await page.locator('h1').first().waitFor({state: 'visible'});
    }
    async function screenshot(name) {
      await page.screenshot({path: path.join(artifacts, `${viewport.width}-${name}.png`), animations: 'disabled'});
    }
    async function checkLayout(name) {
      const size = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth}));
      assert.ok(size.scroll <= size.width + 1, `${name}: horizontal page overflow ${JSON.stringify(size)}`);
      report.checks.push(`${viewport.width}: ${name}`);
    }
    await go('index.html');
    assert.equal(await page.locator('a.location-card').count(), 3);
    await screenshot('portal');
    await checkLayout('portal');

    for (const {slug} of manifest.locations) {
      await go('index.html');
      await page.locator(`a.location-card[href="${slug}/langit_app.html"]`).click();
      await page.waitForURL(`**/${slug}/langit_app.html`);
      assert.equal(await page.locator('.timeline-node').count(), 3);
      for (let day = 0; day < 3; day++) {
        await page.locator(`.timeline-node[data-target="day-${day}"]`).click();
        const panel = page.locator('.day-panel.active');
        assert.equal(await panel.getAttribute('id'), `day-${day}`);
        for (const variable of ['rain', 'temp', 'humidity', 'wind']) {
          const chip = panel.locator(`.chart-chip[data-var="${variable}"]`);
          await chip.click();
          assert.equal(await chip.getAttribute('aria-pressed'), 'true');
          const geometry = await panel.locator('svg.rain-curve').evaluate(svg => ({
            box: svg.getAttribute('viewBox'), bad: /NaN|Infinity/.test(svg.innerHTML)}));
          assert.match(geometry.box, /^0 0 [1-9][\d.]* [1-9][\d.]*$/);
          assert.equal(geometry.bad, false);
        }
      }
      const active = page.locator('.day-panel.active');
      const hourly = active.locator('.hourly-toggle');
      await hourly.click();
      if (viewport.width < 768) {
        const dialog = active.locator('dialog');
        await dialog.waitFor({state: 'visible'});
        assert.equal(await dialog.evaluate(element => element.open), true);
        await screenshot(`${slug}-hourly-dialog`);
        await dialog.locator('.sheet-close').click();
        assert.equal(await dialog.evaluate(element => element.open), false);
      } else {
        assert.equal(await hourly.evaluate(element => element.parentElement.open), true);
        await hourly.click();
      }
      await screenshot(`${slug}-today`);
      await checkLayout(`${slug}: day switching, four chart variables, hourly details`);

      if (viewport.width < 768) {
        await page.locator('.nav-toggle').click();
        assert.equal(await page.locator('.nav-toggle').getAttribute('aria-expanded'), 'true');
      }
      await page.locator('#site-navigation a[href="langit_3day.html"]').click();
      await page.waitForURL(`**/${slug}/langit_3day.html`);
      for (const product of ['langit_3day.html', 'langit_activity.html', 'keandalan_data.html',
                              'akurasi_data.html', 'langit_planner.html']) {
        await go(`${slug}/${product}`);
        if (product === 'langit_planner.html') {
          await page.locator('#act').selectOption('Motor');
          await page.locator('#hh').selectOption({index: 0});
          await page.getByRole('button', {name: 'Cek', exact: true}).click();
          assert.notEqual(await page.locator('#plannerOut').textContent(), 'Cek sebelum berangkat.');
        }
        await screenshot(`${slug}-${product.replace('.html', '')}`);
        await checkLayout(`${slug}/${product}`);
      }
    }

    for (const route of ['langit_portal_map.html', ...manifest.locations.map(x => `${x.slug}/langit_map_room.html`)]) {
      await go(route);
      assert.ok(await page.locator('.time-pill').count() > 0, 'map timeline exists');
      assert.ok(await page.locator('.leaflet-marker-icon').count() > 0, 'real Leaflet forecast markers render');
      const options = await page.locator('#map-location option').evaluateAll(elements => elements.map(x => x.value));
      assert.equal(options.length, route === 'langit_portal_map.html' ? 3 : 1);
      await page.locator('.time-pill').first().click();
      for (const slug of options) {
        await page.locator('#map-location').selectOption(slug);
        const api = JSON.parse(await fs.readFile(path.join(site, slug, 'langit_api_v1.json'), 'utf8'));
        assert.ok((await page.locator('#hud-location-name').textContent()).includes(api.location_name));
      }
      for (const layer of ['risiko', 'temp', 'rain', 'wind', 'humidity', 'cloud']) {
        const button = page.locator(`.param-tab[data-layer="${layer}"]`);
        await button.click();
        assert.equal(await button.getAttribute('aria-pressed'), 'true');
      }
      await page.locator('.time-pill').last().click();
      await page.locator('#play-btn').click();
      assert.equal(await page.locator('#play-btn').getAttribute('aria-pressed'), 'true');
      await page.locator('#play-btn').click();
      assert.equal(await page.locator('#play-btn').getAttribute('aria-pressed'), 'false');
      const bounds = await page.locator('.hud, .timeline-container').evaluateAll(elements => elements.map(element => {
        const r = element.getBoundingClientRect();
        return {left: r.left, right: r.right, width: innerWidth};
      }));
      assert.ok(bounds.every(r => r.left >= 0 && r.right <= r.width + 1), 'map controls fit the viewport');
      await screenshot(route.replaceAll('/', '-').replace('.html', ''));
      report.checks.push(`${viewport.width}: ${route}: timeline, locations, six layers, play/pause`);
    }

    // Deliberately fail the map library: forecast values and controls must survive.
    await page.route('**/leaflet.js', route => route.abort());
    await go('langit_portal_map.html');
    await page.locator('#map-fallback').waitFor({state: 'visible'});
    assert.equal(await page.locator('#map-location option').count(), 3);
    await page.locator('.time-pill').first().click();
    assert.notEqual(await page.locator('#val-temp').textContent(), '');
    await screenshot('map-library-failure');
    report.checks.push(`${viewport.width}: Leaflet failure retains forecast data`);
    await context.tracing.stop({path: path.join(artifacts, `${viewport.width}-trace.zip`)});
    await context.close();
    currentContext = null;
    currentPage = null;
  }
  assert.deepEqual(report.pageErrors, [], 'uncaught application errors');
  assert.deepEqual(report.localFailures, [], 'failed local assets');
  report.status = 'passed';
  console.log(`PASS: ${report.checks.length} Chromium browser checks at desktop and mobile sizes`);
}

main().catch(error => {report.status = 'failed'; report.failure = error.stack; process.exitCode = 1; console.error(error);})
  .finally(async () => {
    await fs.mkdir(artifacts, {recursive: true});
    if (report.status === 'failed' && currentPage) {
      await currentPage.screenshot({path: path.join(artifacts, 'failure.png')}).catch(() => {});
      await currentContext.tracing.stop({path: path.join(artifacts, 'failure-trace.zip')}).catch(() => {});
    }
    await fs.writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
    if (browser) await browser.close();
    server.close();
  });
