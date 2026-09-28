// Generic exterior renderer for an exterior model (see ../model.py).
//
// It draws ONLY what the model contains: prisms at their exact coordinates, and styled
// detail (frames, glass, balusters) strictly inside each opening / railing envelope.
// It makes no architectural decisions. After drawing it publishes window.__audit with the
// world-space bounding box of every element so the consistency check can verify that
// nothing was moved or resized.
//
// Query parameters: model=<url> view=front|rear|left|right|perspective|aerial mode=normal|provenance
import * as THREE from 'three';

const q = new URLSearchParams(location.search);
const VIEW = q.get('view') || 'perspective';
const MODE = q.get('mode') || 'normal';
const model = await (await fetch(q.get('model') || 'model.json')).json();

// model (mm, Z up) -> three.js (m, Y up); plan north (+y) is -Z
const V = (p) => new THREE.Vector3(p[0] / 1000, p[2] / 1000, -p[1] / 1000);
const D = (d) => new THREE.Vector3(d[0], d[2] || 0, -d[1]);

// ---------------------------------------------------------------- renderer
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(innerWidth, innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();

// ---------------------------------------------------------------- material library (aesthetic layer)
function canvasTex(w, h, draw, tile) {
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    draw(c.getContext('2d'), w, h);
    const t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.RepeatWrapping; t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8;
    t.repeat.set(1 / tile[0], 1 / tile[1]);
    return t;
}
function rand(seed) { let s = seed; return () => (s = (s * 16807) % 2147483647) / 2147483647; }
const TEX = {
    board_and_batten: (base, batten) => canvasTex(384, 384, (g, w, h) => {
        g.fillStyle = base; g.fillRect(0, 0, w, h);
        for (let i = 0; i < 3; i++) {
            const x = i * 128;
            g.fillStyle = 'rgba(0,0,0,0.45)'; g.fillRect(x + 12, 0, 3, h);
            g.fillStyle = batten; g.fillRect(x, 0, 12, h);
        }
    }, [1.2, 1.2]),
    lap: (base) => canvasTex(256, 256, (g, w, h) => {
        g.fillStyle = base; g.fillRect(0, 0, w, h);
        for (let y = 0; y < h; y += 32) { g.fillStyle = 'rgba(0,0,0,0.18)'; g.fillRect(0, y, w, 3); }
    }, [1.2, 1.2]),
    wood: (base, seed) => canvasTex(512, 512, (g, w, h) => {
        const r = rand(seed), boards = 7;
        for (let i = 0; i < boards; i++) {
            const d = (r() - 0.5) * 26;
            g.fillStyle = `rgb(${base.map((c, k) => Math.max(0, Math.min(255, c + d * (1 - k * 0.25)))).join(',')})`;
            const b = i * h / boards; g.fillRect(0, b, w, h / boards);
            for (let k = 0; k < 30; k++) {
                g.strokeStyle = `rgba(60,30,10,${0.05 + r() * 0.07})`; g.lineWidth = 1 + r();
                const o = b + r() * h / boards; g.beginPath(); g.moveTo(0, o);
                g.bezierCurveTo(w * .3, o + (r() - .5) * 6, w * .6, o + (r() - .5) * 6, w, o); g.stroke();
            }
            g.fillStyle = 'rgba(25,12,4,0.8)'; g.fillRect(0, b, w, 3);
        }
    }, [0.98, 0.98]),
    seams: (base) => canvasTex(128, 16, (g, w, h) => {
        g.fillStyle = base; g.fillRect(0, 0, w, h);
        g.fillStyle = 'rgba(0,0,0,0.45)'; g.fillRect(0, 0, 5, h);
        g.fillStyle = 'rgba(255,255,255,0.12)'; g.fillRect(5, 0, 3, h);
    }, [0.45, 0.45]),
    shingle: (base) => canvasTex(256, 256, (g, w, h) => {
        const r = rand(9); g.fillStyle = base; g.fillRect(0, 0, w, h);
        for (let y = 0; y < h; y += 32) for (let x = (y / 32) % 2 * 24; x < w; x += 48) {
            const v = (r() - .5) * 20; g.fillStyle = `rgba(${128 + v},${128 + v},${128 + v},0.12)`; g.fillRect(x, y, 46, 30);
            g.fillStyle = 'rgba(0,0,0,0.35)'; g.fillRect(x, y + 30, 48, 2);
        }
    }, [0.9, 0.9]),
    noise: (base, spec, tile) => canvasTex(256, 256, (g, w, h) => {
        const r = rand(5); g.fillStyle = base; g.fillRect(0, 0, w, h);
        for (let i = 0; i < 5000; i++) { g.fillStyle = spec(r()); g.fillRect(r() * w, r() * h, 2, 2); }
    }, tile),
};
const std = (o) => new THREE.MeshStandardMaterial({ side: THREE.DoubleSide, ...o });
const MAT = {
    black_board_and_batten: () => std({ map: TEX.board_and_batten('#1e1e20', '#2c2c30'), roughness: 0.8 }),
    light_lap_siding: () => std({ map: TEX.lap('#d9d6cf'), roughness: 0.85 }),
    charcoal_standing_seam: () => std({ map: TEX.seams('#34373b'), roughness: 0.6, metalness: 0.3 }),
    charcoal_asphalt_shingle: () => std({ map: TEX.shingle('#3a3b3d'), roughness: 0.95 }),
    cedar_decking: () => std({ map: TEX.wood([138, 96, 64], 11), roughness: 0.75 }),
    cedar: () => std({ map: TEX.wood([168, 112, 66], 23), roughness: 0.65 }),
    cedar_carriage_garage_door: () => std({ map: TEX.wood([160, 105, 62], 31), roughness: 0.6 }),
    white_panel_garage_door: () => std({ color: 0xf1f1ee, roughness: 0.6 }),
    black_trim: () => std({ color: 0x0f0f10, roughness: 0.5, metalness: 0.2 }),
    white_trim: () => std({ color: 0xf4f4f1, roughness: 0.6 }),
    painted_door: () => std({ color: 0x2f4858, roughness: 0.5 }),
    black_metal: () => std({ color: 0x111112, roughness: 0.35, metalness: 0.7 }),
    concrete: () => std({ map: TEX.noise('#b8b5ad', (v) => `rgba(${150 + v * 70 | 0},${150 + v * 70 | 0},${145 + v * 70 | 0},0.25)`, [3, 3]), roughness: 0.9 }),
    grass: () => std({ map: TEX.noise('#62783f', (v) => `rgba(${60 + v * 60 | 0},${90 + v * 60 | 0},${40 + v * 30 | 0},0.4)`, [3, 3]), roughness: 1 }),
    glass: () => new THREE.MeshPhysicalMaterial({ color: 0x86a0b2, roughness: 0.08, metalness: 0.3, clearcoat: 1 }),
};
const PROV = {
    plan: () => std({ color: 0xd9d9d9, roughness: 0.9 }),
    derived: () => std({ color: 0x6fa8dc, roughness: 0.9 }),
    inferred: () => std({ color: 0xf29b38, roughness: 0.9 }),
};
const cache = {};
function material(name, klass) {
    if (MODE === 'provenance') return cache['p_' + klass] ||= PROV[klass]();
    return cache[name] ||= (MAT[name] || (() => std({ color: 0xff00ff })))();
}

// ---------------------------------------------------------------- geometry
// Prism: planar polygon (model coords) extruded by a vector. Caps triangulated for concave shapes.
function prismGeometry(points, extrude, uAxis) {
    const P = points.map(V), E = D(extrude).multiplyScalar(1 / 1000);
    const n = new THREE.Vector3();
    for (let i = 0; i < P.length; i++) {  // Newell normal
        const a = P[i], b = P[(i + 1) % P.length];
        n.x += (a.y - b.y) * (a.z + b.z); n.y += (a.z - b.z) * (a.x + b.x); n.z += (a.x - b.x) * (a.y + b.y);
    }
    n.normalize();
    const e1 = P[1].clone().sub(P[0]).normalize(), e2 = n.clone().cross(e1);
    const tris = THREE.ShapeUtils.triangulateShape(P.map(p => new THREE.Vector2(p.dot(e1), p.dot(e2))), []);
    const U = uAxis ? D(uAxis).normalize() : null;
    const pos = [], uv = [];
    const face = (a, b, c) => {
        const fn = b.clone().sub(a).cross(c.clone().sub(a)).normalize();
        let tu, tv;
        if (Math.abs(fn.y) > 0.6) {  // roof/floor-like face: planar mapping along the given axis
            tu = U || new THREE.Vector3(1, 0, 0); tv = new THREE.Vector3(0, 1, 0).cross(tu).normalize();
        } else {                      // wall-like face: horizontal tangent + height
            tu = new THREE.Vector3(fn.z, 0, -fn.x).normalize(); tv = new THREE.Vector3(0, 1, 0);
        }
        [a, b, c].forEach(p => { pos.push(p.x, p.y, p.z); uv.push(p.dot(tu), p.dot(tv)); });
    };
    const B = P.map(p => p.clone().add(E));
    tris.forEach(([i, j, k]) => { face(P[i], P[j], P[k]); face(B[i], B[k], B[j]); });
    for (let i = 0; i < P.length; i++) {
        const j = (i + 1) % P.length;
        face(P[i], B[i], B[j]); face(P[i], B[j], P[j]);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    g.computeVertexNormals();
    return g;
}
function mesh(geo, mat, parent) {
    const m = new THREE.Mesh(geo, mat);
    m.castShadow = m.receiveShadow = true;
    parent.add(m);
    return m;
}
// Box in an opening's local frame: s along the wall, z up from the sill, d across (outward +)
function localBox(env, s0, s1, z0, z1, d0, d1, mat, parent) {
    const o = env.origin, u = env.u, n = env.n;
    const at = (s, z, d) => [o[0] + u[0] * s + n[0] * d, o[1] + u[1] * s + n[1] * d, o[2] + z];
    mesh(prismGeometry([at(s0, z0, d0), at(s1, z0, d0), at(s1, z1, d0), at(s0, z1, d0)],
        [n[0] * (d1 - d0), n[1] * (d1 - d0), 0], u), mat, parent);
}

function drawOpening(e, parent) {
    const g = e.geom, W = g.width, H = g.height, t = g.depth;
    const frameMat = material(e.material, e.class);
    const glass = MODE === 'provenance' ? material('', e.class) : material('glass', e.class);
    const fw = 55, fd = Math.min(t * 0.7, 110);
    const frame = () => {
        localBox(g, 0, W, 0, fw, -fd / 2, fd / 2, frameMat, parent);
        localBox(g, 0, W, H - fw, H, -fd / 2, fd / 2, frameMat, parent);
        localBox(g, 0, fw, 0, H, -fd / 2, fd / 2, frameMat, parent);
        localBox(g, W - fw, W, 0, H, -fd / 2, fd / 2, frameMat, parent);
    };
    switch (e.style) {
        case 'window':
            localBox(g, 0, W, 0, H, -10, 10, glass, parent); frame();
            if (W > 1100) localBox(g, W / 2 - 25, W / 2 + 25, 0, H, -fd / 2, fd / 2, frameMat, parent);
            localBox(g, -40, W + 40, -40, 0, -t / 2, t / 2 + 50, frameMat, parent);  // sill
            break;
        case 'glass_door':
            localBox(g, 0, W, 0, H, -10, 10, glass, parent); frame();
            localBox(g, W / 2 - 30, W / 2 + 30, 0, H, -fd / 2, fd / 2, frameMat, parent);
            break;
        case 'vehicle_door': {
            localBox(g, 0, W, 0, H, -25, 25, frameMat, parent);
            const rows = 4, ph = H / rows;
            const groove = material('black_trim', e.class);
            for (let i = 1; i < rows; i++) localBox(g, 0, W, i * ph - 12, i * ph + 12, 25, 30, groove, parent);
            const lites = 5, lw = W / lites;
            for (let i = 0; i < lites; i++) localBox(g, i * lw + 70, (i + 1) * lw - 70, H - ph + 100, H - 120, 25, 30, glass, parent);
            break;
        }
        case 'entry_door':
            localBox(g, 0, W, 0, H, -22, 22, frameMat, parent);
            localBox(g, W * 0.62, W * 0.8, 300, H - 300, 22, 26, glass, parent);
            break;
        default:
            localBox(g, 0, W, 0, H, -22, 22, frameMat, parent);
    }
}

function drawRailing(e, parent) {
    const g = e.geom, mat = material(e.material, e.class), H = g.height;
    const P = g.path;
    for (let i = 0; i < P.length - 1; i++) {
        const a = P[i], b = P[i + 1];
        const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
        const u = [(b[0] - a[0]) / L, (b[1] - a[1]) / L, 0], n = [-u[1], u[0], 0];
        const env = { origin: a, u, n };
        localBox(env, 0, L, H - 50, H, -30, 30, mat, parent);       // top rail
        localBox(env, 0, L, 60, 100, -20, 20, mat, parent);         // bottom rail
        const posts = Math.max(1, Math.round(L / 1800));
        for (let k = 0; k <= posts; k++) { const s = Math.min(L - 35, Math.max(0, k * L / posts - 35)); localBox(env, s, s + 70, 0, H, -30, 30, mat, parent); }
        const nb = Math.floor(L / 110);
        for (let k = 0; k < nb; k++) { const s = (k + 0.5) * L / nb; localBox(env, s - 9, s + 9, 100, H - 50, -9, 9, mat, parent); }
    }
}

// ---------------------------------------------------------------- build the scene
const audit = [];
const siteKinds = new Set(['ground', 'driveway']);
for (const e of model.elements) {
    const group = new THREE.Group();
    group.userData.id = e.id;
    if (e.geom.type === 'prism') mesh(prismGeometry(e.geom.points, e.geom.extrude, e.geom.u_axis), material(e.material, e.class), group);
    else if (e.geom.type === 'opening') drawOpening(e, group);
    else if (e.geom.type === 'railing') drawRailing(e, group);
    if (siteKinds.has(e.kind)) group.traverse(o => { o.castShadow = false; });
    scene.add(group);
    const b = new THREE.Box3().setFromObject(group);
    audit.push({ id: e.id, min: [b.min.x * 1000, -b.max.z * 1000, b.min.y * 1000], max: [b.max.x * 1000, -b.min.z * 1000, b.max.y * 1000] });
}
window.__audit = { elements: audit };

// ---------------------------------------------------------------- camera + light per view
const v = model.views[VIEW];
const building = new THREE.Box3();
scene.children.forEach(o => { if (!siteKinds.has((model.elements.find(e => e.id === o.userData.id) || {}).kind)) building.expandByObject(o); });
let camera;
const target = V(v.target);
if (v.type === 'orthographic') {
    const dir = D(v.direction).normalize();
    const up = new THREE.Vector3(0, 1, 0), right = up.clone().cross(dir).normalize();  // camera +X
    const corners = [];
    for (const x of [building.min.x, building.max.x]) for (const y of [building.min.y, building.max.y]) for (const z of [building.min.z, building.max.z]) corners.push(new THREE.Vector3(x, y, z));
    const rs = corners.map(c => c.clone().sub(target).dot(right)), us = corners.map(c => c.clone().sub(target).dot(up));
    let w = (Math.max(...rs) - Math.min(...rs)) * 1.12, h = (Math.max(...us) - Math.min(...us)) * 1.25;
    const aspect = innerWidth / innerHeight;
    if (w / h > aspect) h = w / aspect; else w = h * aspect;
    const cr = (Math.max(...rs) + Math.min(...rs)) / 2, cu = (Math.max(...us) + Math.min(...us)) / 2;
    camera = new THREE.OrthographicCamera(cr - w / 2, cr + w / 2, cu + h / 2, cu - h / 2, 0.1, 1000);
    camera.position.copy(target).add(dir.clone().multiplyScalar(80));
    camera.up.copy(up); camera.lookAt(target);
    scene.background = new THREE.Color(0xf7f7f5);
} else {
    camera = new THREE.PerspectiveCamera(v.fov || 36, innerWidth / innerHeight, 0.1, 800);
    camera.position.copy(V(v.position)); camera.lookAt(target);
    const c = document.createElement('canvas'); c.width = 2; c.height = 256;
    const g2 = c.getContext('2d'), gr = g2.createLinearGradient(0, 0, 0, 256);
    gr.addColorStop(0, '#6f9fcf'); gr.addColorStop(0.6, '#bcd4e9'); gr.addColorStop(1, '#e8eef2');
    g2.fillStyle = gr; g2.fillRect(0, 0, 2, 256);
    scene.background = new THREE.CanvasTexture(c); scene.background.colorSpace = THREE.SRGBColorSpace;
    scene.fog = new THREE.Fog(0xd5e1ec, 70, 200);
}
scene.add(new THREE.HemisphereLight(0xe8f0ff, 0x6b7355, MODE === 'provenance' ? 1.6 : 1.15));
// Sun from above-left of the camera so the viewed facade is lit
const camDir = camera.position.clone().sub(target).setY(0).normalize();
const left = new THREE.Vector3(0, 1, 0).cross(camDir).normalize();
const sun = new THREE.DirectionalLight(0xfff3e0, 2.4);
sun.position.copy(target).add(camDir.clone().multiplyScalar(25)).add(left.multiplyScalar(18)).add(new THREE.Vector3(0, 30, 0));
sun.target.position.copy(target);
sun.castShadow = true; sun.shadow.mapSize.set(4096, 4096);
Object.assign(sun.shadow.camera, { left: -35, right: 35, top: 35, bottom: -35, near: 1, far: 150 });
sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02;
scene.add(sun, sun.target);

// ---------------------------------------------------------------- overlay
const ov = document.getElementById('overlay');
ov.querySelector('.title').textContent = model.title || model.plan_id;
ov.querySelector('.view').textContent = v.name + (MODE === 'provenance' ? ' — provenance' : '');
ov.querySelector('.disclaimer').textContent = model.disclaimer;
if (MODE === 'provenance') document.getElementById('legend').classList.remove('hidden');

renderer.render(scene, camera);
window.__ready = true;
