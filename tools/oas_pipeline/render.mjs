// Screenshot every level of an OAS plan in svg-viewer.
// usage: node render.mjs <viewer index.html> <plan.json> <out dir>
// Needs the `playwright` package (resolved from NODE_PATH or a local install).
// Set CHROMIUM_PATH to use a specific browser binary.
import { createRequire } from 'module';
import path from 'path';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const [, , viewer, plan, outDir] = process.argv;
if (!viewer || !plan || !outDir) {
    console.error('usage: node render.mjs <viewer index.html> <plan.json> <out dir>');
    process.exit(2);
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 2000, height: 1400 }, deviceScaleFactor: 1.5 });
const problems = [];
page.on('pageerror', e => problems.push(e.message));
page.on('dialog', d => { problems.push(d.message()); d.dismiss(); });
await page.goto('file://' + path.resolve(viewer));
await page.setInputFiles('#file-upload', path.resolve(plan));
await page.waitForSelector('#svg-wrapper:not(.hidden)');

const base = path.basename(plan, '.json');
const all = await page.$$('#level-selector button');
const buttons = all.length > 1 ? all : [];  // one level: the selector is hidden
const shots = buttons.length ? buttons : [null];
for (const [i, btn] of shots.entries()) {
    if (btn) await btn.click();
    await page.waitForTimeout(300);
    const file = path.join(outDir, buttons.length ? `${base}_level${i + 1}.png` : `${base}.png`);
    await page.screenshot({ path: file });
    console.log('wrote', file);
}
await browser.close();
if (problems.length) {
    console.error('viewer reported:', problems.join('; '));
    process.exit(1);
}
