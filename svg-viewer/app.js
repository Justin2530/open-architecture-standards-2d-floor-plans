document.addEventListener('DOMContentLoaded', () => {
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const fileInput = document.getElementById('file-upload');
    const emptyState = document.getElementById('empty-state');
    const svgWrapper = document.getElementById('svg-wrapper');
    const svgElement = document.getElementById('plan-svg');
    const levelSelector = document.getElementById('level-selector');
    const unitSelector = document.getElementById('unit-selector');

    const EXTERIOR_USAGES = ['porch', 'balcony', 'deck', 'patio', 'terrace', 'exterior'];
    const SERVICE_USAGES = ['garage', 'mechanical', 'utility'];
    const VOID_USAGES = ['void', 'open_to_below'];

    let plan = null;
    let currentLevel = null;   // level id, or null when the plan has no levels
    let units = 'imperial';
    let frame = null;          // drawing extents shared by all levels so floors stay aligned
    let viewBox = { x: 0, y: 0, width: 1000, height: 1000 };
    let originalViewBox = null;
    let isDragging = false;
    let startPan = { x: 0, y: 0 };

    fileInput.addEventListener('change', handleFileUpload);
    document.getElementById('zoom-in').addEventListener('click', () => zoom(1.2));
    document.getElementById('zoom-out').addEventListener('click', () => zoom(1 / 1.2));
    document.getElementById('fit-view').addEventListener('click', fitToContent);
    unitSelector.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
        units = btn.dataset.units;
        unitSelector.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
        if (plan) renderPlan();
    }));

    svgElement.addEventListener('mousedown', startDrag);
    svgElement.addEventListener('mousemove', drag);
    svgElement.addEventListener('mouseup', endDrag);
    svgElement.addEventListener('mouseleave', endDrag);
    svgElement.addEventListener('wheel', handleWheel, { passive: false });

    function handleFileUpload(event) {
        const file = event.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (e) => {
            let parsed;
            try {
                parsed = JSON.parse(e.target.result);
            } catch (error) {
                console.error('Error parsing JSON:', error);
                alert('Invalid JSON file');
                return;
            }
            loadPlan(parsed);
        };
        reader.readAsText(file);
        event.target.value = '';
    }

    // ------------------------------------------------------------------ helpers
    function el(tag, attrs = {}, parent = null) {
        const node = document.createElementNS(SVG_NS, tag);
        Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
        if (parent) parent.appendChild(node);
        return node;
    }

    function sortedLevels() {
        return (plan.levels || []).slice().sort((a, b) => (a.elevation_mm || 0) - (b.elevation_mm || 0));
    }

    function onLevel(item) {
        return !currentLevel || !item.level || item.level === currentLevel;
    }

    function fmtLen(mm) {
        if (units === 'metric') return (mm / 1000).toFixed(2);
        const inches = Math.round(mm / 25.4);
        return `${Math.floor(inches / 12)}'-${inches % 12}"`;
    }

    function fmtDims(w, h) {
        return units === 'metric' ? `${fmtLen(w)} × ${fmtLen(h)} m` : `${fmtLen(w)} × ${fmtLen(h)}`;
    }

    function fmtArea(m2) {
        return units === 'metric' ? `${m2.toFixed(1)} m²` : `${Math.round(m2 * 10.7639).toLocaleString()} sf`;
    }

    function polygonArea(pts) {
        let a = 0;
        pts.forEach((p, i) => { const q = pts[(i + 1) % pts.length]; a += p.x * q.y - q.x * p.y; });
        return Math.abs(a) / 2;
    }

    function axisRect(pts) {
        if (pts.length !== 4) return null;
        const ok = pts.every((p, i) => { const q = pts[(i + 1) % 4]; return p.x === q.x || p.y === q.y; });
        if (!ok) return null;
        const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
        return { w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
    }

    function pointInPolygon(x, y, pts) {
        let inside = false;
        for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
            const a = pts[i], b = pts[j];
            if ((a.y > y) !== (b.y > y) && x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x) inside = !inside;
        }
        return inside;
    }

    function distToSegment(x, y, a, b) {
        const dx = b.x - a.x, dy = b.y - a.y;
        const len2 = dx * dx + dy * dy;
        const t = len2 ? Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / len2)) : 0;
        return Math.hypot(x - (a.x + t * dx), y - (a.y + t * dy));
    }

    // Most open interior point of a polygon (grid search) — keeps labels inside L-shaped rooms.
    function labelAnchor(pts) {
        if (axisRect(pts)) {
            const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
            return { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 };
        }
        const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
        const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
        const N = 40;
        let best = null, bestD = -1;
        for (let i = 0; i <= N; i++) {
            for (let j = 0; j <= N; j++) {
                const x = minX + (maxX - minX) * i / N, y = minY + (maxY - minY) * j / N;
                if (!pointInPolygon(x, y, pts)) continue;
                let d = Infinity;
                pts.forEach((p, k) => { d = Math.min(d, distToSegment(x, y, p, pts[(k + 1) % pts.length])); });
                if (d > bestD) { bestD = d; best = { x, y }; }
            }
        }
        return best || { x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
    }

    // Clear half-spans from a point to the polygon boundary, horizontally and vertically.
    function clearSpans(x, y, pts) {
        let left = Infinity, right = Infinity, down = Infinity, up = Infinity;
        pts.forEach((a, i) => {
            const b = pts[(i + 1) % pts.length];
            if ((a.y > y) !== (b.y > y)) {
                const xi = a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y);
                if (xi >= x) right = Math.min(right, xi - x); else left = Math.min(left, x - xi);
            }
            if ((a.x > x) !== (b.x > x)) {
                const yi = a.y + (x - a.x) * (b.y - a.y) / (b.x - a.x);
                if (yi >= y) up = Math.min(up, yi - y); else down = Math.min(down, y - yi);
            }
        });
        return { w: 2 * Math.min(left, right), h: 2 * Math.min(up, down) };
    }

    function wrapTwoLines(text) {
        const words = text.split(' ');
        if (words.length < 2) return [text];
        let best = null;
        for (let i = 1; i < words.length; i++) {
            const a = words.slice(0, i).join(' '), b = words.slice(i).join(' ');
            const score = Math.max(a.length, b.length);
            if (!best || score < best.score) best = { score, lines: [a, b] };
        }
        return best.lines;
    }

    // ------------------------------------------------------------------ loading
    function loadPlan(parsed) {
        plan = parsed;
        const levels = sortedLevels();
        currentLevel = levels.length ? levels[0].id : null;
        buildLevelSelector(levels);
        frame = computeFrame();
        emptyState.classList.add('hidden');
        svgWrapper.classList.remove('hidden');
        renderPlan();
        fitToContent();
    }

    function buildLevelSelector(levels) {
        levelSelector.innerHTML = '';
        levelSelector.classList.toggle('hidden', levels.length < 2);
        levels.forEach(level => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = level.name || level.id;
            btn.dataset.level = level.id;
            btn.setAttribute('role', 'tab');
            btn.classList.toggle('active', level.id === currentLevel);
            btn.addEventListener('click', () => {
                currentLevel = level.id;
                levelSelector.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
                renderPlan();
            });
            levelSelector.appendChild(btn);
        });
    }

    function computeFrame() {
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        const add = (p, pad = 0) => {
            minX = Math.min(minX, p.x - pad); maxX = Math.max(maxX, p.x + pad);
            minY = Math.min(minY, p.y - pad); maxY = Math.max(maxY, p.y + pad);
        };
        (plan.rooms || []).forEach(r => (r.boundary_polygon?.points || []).forEach(p => add(p)));
        (plan.walls || []).forEach(w => { add(w.from, (w.thickness_mm || 200) / 2); add(w.to, (w.thickness_mm || 200) / 2); });
        if (!isFinite(minX)) { minX = minY = 0; maxX = maxY = 10000; }
        const span = Math.max(maxX - minX, maxY - minY);
        const text = Math.min(Math.max(span / 110, 110), 320); // base label height in plan mm
        return { minX, minY, maxX, maxY, text, titleHeight: text * 9 };
    }

    // ------------------------------------------------------------------ rendering
    function renderPlan() {
        svgElement.innerHTML = '';
        const defs = el('defs', {}, svgElement);
        const hatch = el('pattern', { id: 'hatch-exterior', patternUnits: 'userSpaceOnUse', width: 260, height: 260,
            patternTransform: 'rotate(45)' }, defs);
        el('rect', { width: 260, height: 260, fill: '#fbfbfa' }, hatch);
        el('line', { x1: 0, y1: 0, x2: 0, y2: 260, stroke: '#cfcfcf', 'stroke-width': 14 }, hatch);

        // Plan geometry lives in OAS coordinates (Y up); flip once at the render boundary.
        const geo = el('g', { id: 'oas-plan', transform: `translate(0, ${frame.maxY}) scale(1, -1)` }, svgElement);
        const gRooms = el('g', { id: 'oas-rooms' }, geo);
        const gWalls = el('g', { id: 'oas-walls' }, geo);
        const gOpenings = el('g', { id: 'oas-openings' }, geo);
        const gCirc = el('g', { id: 'oas-circulation' }, geo);
        const gLabels = el('g', { id: 'oas-labels' }, svgElement); // screen orientation

        const rooms = (plan.rooms || []).filter(onLevel);
        const walls = (plan.walls || []).filter(onLevel);
        const wallsById = Object.fromEntries((plan.walls || []).map(w => [w.id, w]));
        const openings = (plan.openings || []).filter(o => onLevel(o) && wallsById[o.in_wall] && onLevel(wallsById[o.in_wall]));

        rooms.forEach((room, i) => renderRoom(gRooms, defs, room, i));

        const openingsByWall = {};
        openings.forEach(o => (openingsByWall[o.in_wall] = openingsByWall[o.in_wall] || []).push(o));
        walls.forEach(w => renderWall(gWalls, w, openingsByWall[w.id] || []));
        openings.forEach(o => renderOpening(gOpenings, o, wallsById[o.in_wall]));
        (plan.railings || []).filter(onLevel).forEach(r => renderRailing(gCirc, r));

        rooms.forEach(room => renderLabel(gLabels, room));
        renderTitleBlock();
    }

    function roomClass(room) {
        const usage = (room.usage || '').toLowerCase();
        const tags = room.tags || [];
        if (VOID_USAGES.includes(usage) || tags.includes('open_to_below')) return 'room-void';
        if (EXTERIOR_USAGES.includes(usage) || tags.includes('exterior')) return 'room-exterior';
        if (SERVICE_USAGES.includes(usage)) return 'room-service';
        return '';
    }

    function roomSummary(room) {
        const pts = room.boundary_polygon.points;
        const rect = axisRect(pts);
        const area = room.area_m2 != null ? room.area_m2 : polygonArea(pts) / 1e6;
        return { dims: rect ? fmtDims(rect.w, rect.h) : null, area: fmtArea(area) };
    }

    function renderRoom(parent, defs, room, index) {
        if (!room.boundary_polygon || !room.boundary_polygon.points) return;
        const pts = room.boundary_polygon.points;
        const cls = roomClass(room);
        const polygon = el('polygon', { points: pts.map(p => `${p.x},${p.y}`).join(' '),
            class: `room-polygon ${cls}`.trim(), id: room.id }, parent);
        const s = roomSummary(room);
        el('title', {}, polygon).textContent = `${room.name || room.id}${s.dims ? ' — ' + s.dims : ''} (${s.area})`;

        if (cls === 'room-void') {
            // Conventional "open to below" cross, clipped to the room outline.
            const clipId = `clip-${index}`;
            const clip = el('clipPath', { id: clipId }, defs);
            el('polygon', { points: pts.map(p => `${p.x},${p.y}`).join(' ') }, clip);
            const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
            const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
            const g = el('g', { 'clip-path': `url(#${clipId})` }, parent);
            el('line', { x1: x0, y1: y0, x2: x1, y2: y1, class: 'void-cross' }, g);
            el('line', { x1: x0, y1: y1, x2: x1, y2: y0, class: 'void-cross' }, g);
        }
    }

    function renderWall(parent, wall, openings) {
        const dx = wall.to.x - wall.from.x, dy = wall.to.y - wall.from.y;
        const len = Math.hypot(dx, dy);
        if (!len) return;
        const ux = dx / len, uy = dy / len;
        const t = wall.thickness_mm || 200;
        const cuts = openings
            .map(o => ({ start: Math.max(0, o.position_along_wall_mm), end: Math.min(len, o.position_along_wall_mm + o.width_mm) }))
            .sort((a, b) => a.start - b.start);
        const merged = [];
        cuts.forEach(c => {
            const last = merged[merged.length - 1];
            if (last && c.start <= last.end) last.end = Math.max(last.end, c.end); else merged.push({ ...c });
        });
        const segment = (a, b) => el('line', {
            x1: wall.from.x + ux * a, y1: wall.from.y + uy * a, x2: wall.from.x + ux * b, y2: wall.from.y + uy * b,
            'stroke-width': t, class: 'wall-line' }, parent);
        let cursor = 0;
        merged.forEach(c => { if (c.start > cursor) segment(cursor, c.start); cursor = Math.max(cursor, c.end); });
        if (cursor < len) segment(cursor, len);
    }

    function renderOpening(parent, opening, wall) {
        const dx = wall.to.x - wall.from.x, dy = wall.to.y - wall.from.y;
        const len = Math.hypot(dx, dy);
        const ux = dx / len, uy = dy / len;
        const nx = -uy, ny = ux; // left normal of the wall's from->to direction
        const t = wall.thickness_mm || 200;
        const W = opening.width_mm || 0;
        const s = opening.position_along_wall_mm || 0;
        const A = { x: wall.from.x + ux * s, y: wall.from.y + uy * s };
        const B = { x: A.x + ux * W, y: A.y + uy * W };
        const off = (p, k) => ({ x: p.x + nx * k, y: p.y + ny * k });
        const line = (p, q, cls, g) => el('line', { x1: p.x, y1: p.y, x2: q.x, y2: q.y, class: cls }, g);

        const g = el('g', { class: 'opening-group', id: opening.id }, parent);
        const op = (opening.operation || '').toLowerCase();
        el('title', {}, g).textContent = `${opening.type_name || opening.opening_type}${op ? ` (${op})` : ''} — ${fmtLen(W)}${units === 'metric' ? ' m' : ''}`;
        el('line', { x1: A.x, y1: A.y, x2: B.x, y2: B.y, 'stroke-width': t + 6, class: 'opening-gap' }, g);
        // jambs
        line(off(A, t / 2), off(A, -t / 2), 'door-jamb', g);
        line(off(B, t / 2), off(B, -t / 2), 'door-jamb', g);

        if (opening.opening_type === 'window') {
            line(off(A, t / 2), off(B, t / 2), 'window-line', g);
            line(A, B, 'window-line', g);
            line(off(A, -t / 2), off(B, -t / 2), 'window-line', g);
            return;
        }
        if (opening.opening_type !== 'door') {
            line(A, B, 'door-overhead', g);
            return;
        }
        if (op === 'swing') {
            const hingeAtEnd = opening.hinge_side === 'right';
            const hinge = hingeAtEnd ? B : A;
            const closed = hingeAtEnd ? A : B;
            const dir = opening.swing_direction === 'outward' ? -1 : 1;
            const open = off(hinge, dir * W);
            line(hinge, open, 'door-leaf', g);
            const cross = (closed.x - hinge.x) * (open.y - hinge.y) - (closed.y - hinge.y) * (open.x - hinge.x);
            el('path', { d: `M ${closed.x} ${closed.y} A ${W} ${W} 0 0 ${cross > 0 ? 1 : 0} ${open.x} ${open.y}`,
                class: 'door-swing-arc' }, g);
        } else if (op === 'sliding' || op === 'slide' || op === 'folding') {
            const panel = W * 0.55, k = t / 5;
            line(off(A, k), off({ x: A.x + ux * panel, y: A.y + uy * panel }, k), 'door-leaf', g);
            line(off({ x: B.x - ux * panel, y: B.y - uy * panel }, -k), off(B, -k), 'door-leaf', g);
        } else {
            // Overhead / unspecified operation: dashed line across the opening.
            line(A, B, 'door-overhead', g);
        }
    }

    function renderRailing(parent, railing) {
        const pts = (railing.path?.points || []).map(p => `${p.x},${p.y}`).join(' ');
        if (!pts) return;
        const g = el('g', { class: 'railing', id: railing.id }, parent);
        el('title', {}, g).textContent = railing.type_name || 'Railing';
        // Thin double line: dark stroke with a lighter core.
        el('polyline', { points: pts, fill: 'none', stroke: '#111', 'stroke-width': 70, 'stroke-linejoin': 'miter' }, g);
        el('polyline', { points: pts, fill: 'none', stroke: '#fff', 'stroke-width': 40, 'stroke-linejoin': 'miter' }, g);
    }

    function renderLabel(parent, room) {
        const pts = room.boundary_polygon?.points;
        if (!pts || pts.length < 3) return;
        const anchor = labelAnchor(pts);
        const spans = clearSpans(anchor.x, anchor.y, pts);
        const sx = anchor.x, sy = frame.maxY - anchor.y; // screen coordinates
        const name = room.name || room.id;
        const s = roomSummary(room);
        const dims = roomClass(room) === 'room-void' ? null : (s.dims || s.area);
        const base = frame.text;
        const margin = base * 0.7;
        const availW = spans.w - margin, availH = spans.h - margin;

        const g = el('g', { class: 'room-label-group' }, parent);
        const build = (nameLines, scale) => {
            g.innerHTML = '';
            const nameSize = base * scale, dimSize = base * 0.8 * scale;
            const lines = nameLines.map(txt => ({ txt, size: nameSize, cls: 'room-label' }));
            if (dims) lines.push({ txt: dims, size: dimSize, cls: 'room-dims' });
            const gap = nameSize * 0.25;
            const total = lines.reduce((h, l) => h + l.size, 0) + gap * (lines.length - 1);
            let y = sy - total / 2;
            lines.forEach(l => {
                y += l.size;
                const text = el('text', { x: sx, y: y - l.size * 0.18, class: l.cls, 'font-size': l.size,
                    stroke: '#fff', 'stroke-width': l.size * 0.22, 'paint-order': 'stroke', 'stroke-linejoin': 'round' }, g);
                text.textContent = l.txt;
                y += gap;
            });
            const bb = g.getBBox();
            return { w: bb.width, h: bb.height };
        };

        const options = [];
        [[name], wrapTwoLines(name)].forEach((nameLines, wrapped) => {
            [false, true].forEach(rotated => {
                const size = build(nameLines, 1);
                const [aw, ah] = rotated ? [availH, availW] : [availW, availH];
                const scale = Math.min(1, aw / size.w, ah / size.h);
                options.push({ nameLines, rotated, scale: scale - (wrapped ? 0.02 : 0) - (rotated ? 0.05 : 0) });
            });
        });
        options.sort((a, b) => b.scale - a.scale);
        const pick = options[0];
        build(pick.nameLines, Math.max(pick.scale, 0.4));
        if (pick.rotated) g.setAttribute('transform', `rotate(-90 ${sx} ${sy})`);
    }

    function renderTitleBlock() {
        const levels = sortedLevels();
        const level = levels.find(l => l.id === currentLevel);
        const T = frame.text;
        const top = frame.maxY - frame.minY + T * 3;
        const left = frame.minX;
        const g = el('g', { class: 'title-block' }, svgElement);

        let title = (level ? (level.name || level.id) : (plan.title || 'Floor')).toUpperCase();
        if (!/\bPLAN$/.test(title)) title += ' PLAN';
        el('text', { x: left, y: top + T * 2, class: 'title-main', 'font-size': T * 2.2 }, g).textContent = title;
        const sub = [plan.title, level && level.elevation_mm != null ? `Level elevation ${fmtLen(level.elevation_mm)}${units === 'metric' ? ' m' : ''}` : null,
            'Room dimensions to wall centerlines', 'CONCEPT DESIGN — NOT FOR CONSTRUCTION'].filter(Boolean).join('   ·   ');
        el('text', { x: left, y: top + T * 3.6, class: 'title-sub', 'font-size': T * 0.95 }, g).textContent = sub;

        // Graphic scale bar
        // Longest "nice" length that fits in ~30% of the plan width, ticked at 0, 1/4, 1/2 and full.
        const unitMm = units === 'metric' ? 1000 : 304.8;
        const nice = units === 'metric' ? [1, 2, 4, 8, 10, 20, 40] : [2, 4, 8, 10, 20, 40, 80];
        const maxLen = (frame.maxX - frame.minX) * 0.3;
        const total = nice.filter(n => n * unitMm <= maxLen).pop() || nice[0];
        const ticks = [0, total / 4, total / 2, total];
        const steps = ticks.map(v => v * unitMm);
        const labels = ticks.map((v, i) => i === 0 ? '0' : units === 'metric'
            ? `${+v.toFixed(2)}${i === ticks.length - 1 ? ' m' : ''}` : `${+v.toFixed(2)}'`);
        const barX = frame.maxX - steps[steps.length - 1] - T * 6, barY = top + T * 1.2, barH = T * 0.45;
        const sb = el('g', { class: 'scale-bar' }, g);
        for (let i = 0; i < steps.length - 1; i++) {
            el('rect', { x: barX + steps[i], y: barY, width: steps[i + 1] - steps[i], height: barH,
                fill: i % 2 ? '#fff' : '#111' }, sb);
        }
        steps.forEach((st, i) => {
            el('text', { x: barX + st, y: barY + barH + T * 1.1, 'font-size': T * 0.8, 'text-anchor': 'middle' }, sb).textContent = labels[i];
        });

        // North arrow (+Y in OAS is north, which renders up)
        const nx = frame.maxX - T * 2.2, ny = top + T * 1.4, r = T * 1.3;
        el('circle', { cx: nx, cy: ny, r, fill: 'none', stroke: '#111', 'stroke-width': T * 0.08 }, g);
        el('path', { d: `M ${nx} ${ny - r * 0.95} L ${nx + r * 0.45} ${ny + r * 0.6} L ${nx} ${ny + r * 0.3} L ${nx - r * 0.45} ${ny + r * 0.6} Z`, fill: '#111' }, g);
        el('text', { x: nx, y: ny + r + T * 1.1, 'font-size': T * 0.9, 'text-anchor': 'middle', 'font-weight': 700 }, g).textContent = 'N';
    }

    // ------------------------------------------------------------------ view
    function fitToContent() {
        if (!frame) return;
        const pad = frame.text * 5;
        viewBox = {
            x: frame.minX - pad,
            y: -pad,
            width: (frame.maxX - frame.minX) + pad * 2,
            height: (frame.maxY - frame.minY) + frame.titleHeight + pad * 1.5,
        };
        originalViewBox = { ...viewBox };
        updateViewBox();
    }

    function updateViewBox() {
        svgElement.setAttribute('viewBox', `${viewBox.x} ${viewBox.y} ${viewBox.width} ${viewBox.height}`);
    }

    function toSvgPoint(clientX, clientY) {
        const ctm = svgElement.getScreenCTM();
        if (!ctm) return { x: viewBox.x + viewBox.width / 2, y: viewBox.y + viewBox.height / 2 };
        const pt = svgElement.createSVGPoint();
        pt.x = clientX; pt.y = clientY;
        return pt.matrixTransform(ctm.inverse());
    }

    function zoom(factor, center) {
        const c = center || { x: viewBox.x + viewBox.width / 2, y: viewBox.y + viewBox.height / 2 };
        viewBox.x = c.x - (c.x - viewBox.x) / factor;
        viewBox.y = c.y - (c.y - viewBox.y) / factor;
        viewBox.width /= factor;
        viewBox.height /= factor;
        updateViewBox();
    }

    function startDrag(e) {
        isDragging = true;
        startPan = { x: e.clientX, y: e.clientY };
    }

    function drag(e) {
        if (!isDragging) return;
        const ctm = svgElement.getScreenCTM();
        const k = ctm ? ctm.a : 1;
        viewBox.x -= (e.clientX - startPan.x) / k;
        viewBox.y -= (e.clientY - startPan.y) / k;
        startPan = { x: e.clientX, y: e.clientY };
        updateViewBox();
    }

    function endDrag() {
        isDragging = false;
    }

    function handleWheel(e) {
        e.preventDefault();
        zoom(e.deltaY > 0 ? 1 / 1.1 : 1.1, toSvgPoint(e.clientX, e.clientY));
    }
});
