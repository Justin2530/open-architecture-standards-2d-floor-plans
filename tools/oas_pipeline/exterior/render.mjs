// Render an exterior model with viewer/ in headless Chromium and write one PNG per view
// plus audit.json (world-space bounds of everything actually drawn).
// usage: node render.mjs <exterior_model.json> <out dir> [view ...]
// Needs `npm install` in this directory (three) and the `playwright` package.
import { createRequire } from 'module';
import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const [, , modelPath, outDir, ...only] = process.argv;
if (!modelPath || !outDir) { console.error('usage: node render.mjs <model.json> <out dir> [view ...]'); process.exit(2); }
const threeDir = path.join(HERE, 'node_modules', 'three');
if (!fs.existsSync(path.join(threeDir, 'build', 'three.module.js'))) { console.error('three.js not installed: run npm install in ' + HERE); process.exit(2); }
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' };

const server = http.createServer((req, res) => {
    const url = decodeURIComponent(req.url.split('?')[0]);
    let file;
    if (url === '/model.json') file = path.resolve(modelPath);
    else if (url.startsWith('/three/')) file = path.join(threeDir, url.slice(7));
    else file = path.join(HERE, 'viewer', url === '/' ? 'index.html' : url);
    fs.readFile(file, (err, data) => {
        if (err) { res.writeHead(404); res.end(); return; }
        res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
        res.end(data);
    });
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

const shots = [
    ['perspective', 'normal'], ['front', 'normal'], ['rear', 'normal'], ['left', 'normal'], ['right', 'normal'],
    ['aerial', 'normal'], ['perspective', 'provenance'], ['aerial', 'provenance'],
].filter(([v]) => !only.length || only.includes(v));

const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const problems = [];
page.on('pageerror', e => { problems.push(e.message); console.error('page error:', e.message); });
page.on('console', m => { if (m.type() === 'error') console.error('console:', m.text()); });
fs.mkdirSync(outDir, { recursive: true });
let audit = null;
for (const [view, mode] of shots) {
    await page.goto(`${base}/index.html?model=/model.json&view=${view}&mode=${mode}`);
    await page.waitForFunction(() => window.__ready === true || window.__failed, null, { timeout: 300000 });
    if (problems.length) break;
    const file = path.join(outDir, `${view}${mode === 'provenance' ? '_provenance' : ''}.png`);
    await page.screenshot({ path: file });
    console.log('wrote', file);
    audit ||= await page.evaluate(() => window.__audit);
}
fs.writeFileSync(path.join(outDir, 'audit.json'), JSON.stringify(audit));
console.log('wrote', path.join(outDir, 'audit.json'));
await browser.close();
server.close();
if (problems.length) { console.error('renderer errors:', problems.join('; ')); process.exit(1); }
