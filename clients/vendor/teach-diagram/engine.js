/*!
 * js-teaching-diagram engine v1.0
 * 零依赖原生 JS：把教学图 JSON 规格渲染为 SVG，并支持「边讲边画」分步演示。
 * 用法：TeachDiagram.mount(spec, { stage, controls... })
 */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var FONT = '"PingFang SC","Microsoft YaHei","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Segoe UI",system-ui,sans-serif';

  /* ============================ 主题 ============================ */

  var THEMES = {
    light: {
      bg: '#FBFAF7', ink: '#1B2430', sub: '#5A6B7B', faint: '#8D9AA8',
      line: '#B9C4D0', card: '#FFFFFF', cardStroke: '#D8E0E8', soft: '#EEF3FA',
      a1: '#2F6FED', a2: '#E05252', a3: '#2E9E6B', a4: '#E8A33D', note: '#FFF6E0'
    },
    chalk: {
      bg: '#1F2A28', ink: '#F1F5EF', sub: '#A9BDB6', faint: '#7E948D',
      line: '#4E6660', card: '#26332F', cardStroke: '#3E5450', soft: '#2B3A35',
      a1: '#F2C14E', a2: '#EF7E6A', a3: '#7FD1A8', a4: '#8FB8F0', note: '#3A3326'
    },
    blueprint: {
      bg: '#0E2A47', ink: '#E8F1FA', sub: '#93B2CC', faint: '#6A8AA6',
      line: '#3A6B93', card: '#123A5E', cardStroke: '#2E6690', soft: '#164A75',
      a1: '#4FC3F7', a2: '#FF8A65', a3: '#8CE99A', a4: '#FFD54F', note: '#1B4166'
    },
    ink: {
      bg: '#F6F2E7', ink: '#22201C', sub: '#6B6357', faint: '#9A9182',
      line: '#C9BFA8', card: '#FFFDF7', cardStroke: '#DDD3BE', soft: '#EFE8D6',
      a1: '#C0392B', a2: '#2F6FED', a3: '#4A7C59', a4: '#C99A2E', note: '#FBF0D8'
    }
  };

  var CANVAS = {
    portrait: { w: 1080, h: 1920 },
    landscape: { w: 1920, h: 1080 },
    square: { w: 1080, h: 1080 }
  };

  /* ============================ 测量 ============================ */

  var _mctx = null;
  function mctx() {
    if (!_mctx) {
      var c = document.createElement('canvas');
      _mctx = c.getContext('2d');
    }
    return _mctx;
  }

  function textW(str, size, weight) {
    var c = mctx();
    c.font = (weight || 400) + ' ' + size + 'px ' + FONT;
    return c.measureText(str).width;
  }

  var NO_START = '，。、；：？！）】」』”’%,.;:?!)]}';
  var NO_END = '（【「『“‘([{';

  function wrapText(text, maxW, size, weight) {
    var raw = String(text == null ? '' : text).split('\n');
    var out = [];
    for (var p = 0; p < raw.length; p++) {
      var para = raw[p];
      if (!para) { out.push(''); continue; }
      if (textW(para, size, weight) <= maxW) { out.push(para); continue; }
      var line = '';
      for (var i = 0; i < para.length; i++) {
        var ch = para[i];
        if (line && textW(line + ch, size, weight) > maxW) {
          var cut = line.length;
          if (NO_START.indexOf(ch) >= 0 && cut > 1) {
            cut = line.length - 1;
          } else if (NO_END.indexOf(line[line.length - 1]) >= 0 && cut > 1) {
            cut = line.length - 1;
          } else {
            var sp = line.lastIndexOf(' ');
            if (sp > 0 && /[A-Za-z0-9]$/.test(line)) cut = sp;
          }
          out.push(line.slice(0, cut).replace(/\s+$/, ''));
          line = line.slice(cut).replace(/^\s+/, '') + ch;
        } else {
          line += ch;
        }
      }
      if (line) out.push(line);
    }
    return out;
  }

  /* ============================ 基础工具 ============================ */

  function el(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) if (attrs[k] != null && attrs[k] !== '') n.setAttribute(k, attrs[k]);
    return n;
  }

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }

  var _seed = 20261022;
  function rnd() { _seed = (_seed * 1103515245 + 12345) & 0x7fffffff; return _seed / 0x7fffffff; }
  function seedReset(s) { _seed = s || 20261022; }

  function pathLen(d) {
    var p = el('path', { d: d });
    if (!p.getTotalLength) return 0;
    try { return p.getTotalLength(); } catch (e) { return 0; }
  }

  /* ============================ 渲染上下文 ============================ */

  var S = null;

  function resetState(spec, theme) {
    S = {
      spec: spec,
      T: theme,
      items: [],
      byOwner: {},
      defs: null,
      markers: {},
      W: 0, H: 0, pad: 0
    };
  }

  function reg(id, node, opts) {
    if (!id) return;
    opts = opts || {};
    S.items.push({
      id: id,
      node: node,
      kind: opts.kind || 'fade',
      owner: opts.owner || null,
      step: -1
    });
  }

  function ensureMarker(color) {
    var key = 'ar' + String(color).replace(/[^a-zA-Z0-9]/g, '');
    if (S.markers[key]) return S.markers[key];
    var m = el('marker', {
      id: key, viewBox: '0 0 10 10', refX: '8.5', refY: '5',
      markerWidth: '4.2', markerHeight: '4.2', orient: 'auto-start-reverse',
      markerUnits: 'strokeWidth'
    });
    m.appendChild(el('path', { d: 'M0,0 L10,5 L0,10 z', fill: color }));
    S.defs.appendChild(m);
    S.markers[key] = key;
    return key;
  }

  /* ============================ 绘制原语 ============================ */

  function rect(g, o) {
    var r = el('rect', {
      x: o.x, y: o.y, width: o.w, height: o.h,
      rx: o.r == null ? 14 : o.r, ry: o.r == null ? 14 : o.r,
      fill: o.fill || 'none', stroke: o.stroke || 'none',
      'stroke-width': o.sw || 0, 'stroke-dasharray': o.dash || null,
      opacity: o.opacity == null ? null : o.opacity
    });
    g.appendChild(r);
    return r;
  }

  // 返回 { h, w }
  function textBlock(g, text, x, y, o) {
    o = o || {};
    var size = o.size || 32;
    var weight = o.weight || 400;
    var lh = o.lineH || size * 1.42;
    var maxW = o.maxW || 1e6;
    var lines = Array.isArray(text) ? text : wrapText(text, maxW, size, weight);
    var maxLineW = 0;
    for (var i = 0; i < lines.length; i++) maxLineW = Math.max(maxLineW, textW(lines[i], size, weight));
    var t = el('text', {
      fill: o.fill || '#000', 'font-size': size, 'font-weight': weight,
      'font-family': FONT, 'text-anchor': o.anchor || 'start',
      'letter-spacing': o.ls || null, opacity: o.opacity == null ? null : o.opacity
    });
    for (var j = 0; j < lines.length; j++) {
      var ts = el('tspan', { x: x, y: y + size * 0.86 + j * lh });
      ts.textContent = lines[j] || ' ';
      t.appendChild(ts);
    }
    g.appendChild(t);
    return { h: lines.length * lh, w: maxLineW, lines: lines.length };
  }

  function path(g, o) {
    var p = el('path', {
      d: o.d, fill: o.fill || 'none', stroke: o.stroke || 'none',
      'stroke-width': o.sw || 0, 'stroke-dasharray': o.dash || null,
      'stroke-linecap': o.cap || 'round', 'stroke-linejoin': 'round',
      'marker-end': o.markerEnd || null, opacity: o.opacity == null ? null : o.opacity
    });
    g.appendChild(p);
    return p;
  }

  function circle(g, o) {
    var c = el('circle', {
      cx: o.cx, cy: o.cy, r: o.r, fill: o.fill || 'none',
      stroke: o.stroke || 'none', 'stroke-width': o.sw || 0,
      'stroke-dasharray': o.dash || null
    });
    g.appendChild(c);
    return c;
  }

  // 带箭头的连线（垂直三次贝塞尔）
  function connectV(g, x1, y1, x2, y2, color, o) {
    o = o || {};
    var midY = (y1 + y2) / 2;
    var d = 'M' + x1 + ',' + y1 + ' C' + x1 + ',' + midY + ' ' + x2 + ',' + midY + ' ' + x2 + ',' + y2;
    return path(g, {
      d: d, stroke: color, sw: o.sw || 3.2, dash: o.dash,
      markerEnd: o.arrow === false ? null : 'url(#' + ensureMarker(color) + ')'
    });
  }

  function connectH(g, x1, y1, x2, y2, color, o) {
    o = o || {};
    var midX = (x1 + x2) / 2;
    var d = 'M' + x1 + ',' + y1 + ' C' + midX + ',' + y1 + ' ' + midX + ',' + y2 + ' ' + x2 + ',' + y2;
    return path(g, {
      d: d, stroke: color, sw: o.sw || 3.2, dash: o.dash,
      markerEnd: o.arrow === false ? null : 'url(#' + ensureMarker(color) + ')'
    });
  }

  // 圆角肘形线
  function elbow(g, pts, color, o) {
    o = o || {};
    var r = o.r == null ? 22 : o.r;
    var d = 'M' + pts[0][0] + ',' + pts[0][1];
    for (var i = 1; i < pts.length; i++) {
      var a = pts[i - 1], b = pts[i];
      d += ' L' + b[0] + ',' + b[1];
    }
    return path(g, {
      d: d, stroke: color, sw: o.sw || 3, dash: o.dash || null,
      markerEnd: o.arrow === false ? null : 'url(#' + ensureMarker(color) + ')'
    });
  }

  function labelChip(g, text, x, y, o) {
    o = o || {};
    var size = o.size || 24;
    var w = textW(text, size, o.weight || 500) + size * 1.1;
    var h = size * 1.72;
    var rx = o.anchor === 'middle' ? x - w / 2 : o.anchor === 'end' ? x - w : x;
    rect(g, {
      x: rx, y: y - h / 2, w: w, h: h, r: h / 2,
      fill: o.bg || '#fff', stroke: o.stroke || 'none', sw: o.sw || 0
    });
    textBlock(g, text, rx + w / 2, y - h / 2 + (h - size * 1.42) / 2, {
      size: size, weight: o.weight || 500, fill: o.fill || '#000', anchor: 'middle'
    });
    return { w: w, h: h, x: rx };
  }

  /* ============================ 图型：flow ============================ */

  function layoutFlow(spec, box) {
    var T = S.T, g = S.root;
    var nodes = spec.nodes || [];
    if (!nodes.length) return;

    var map = {};
    nodes.forEach(function (n, i) { map[n.id] = { n: n, i: i, layer: 0 }; });

    // 最长路径分层（迭代松弛，容忍环）
    var edges = (spec.edges || []).filter(function (e) { return map[e.from] && map[e.to]; });
    for (var it = 0; it < nodes.length; it++) {
      var changed = false;
      for (var e = 0; e < edges.length; e++) {
        var a = map[edges[e].from], b = map[edges[e].to];
        if (b.layer < a.layer + 1 && b.layer < nodes.length) {
          b.layer = a.layer + 1; changed = true;
        }
      }
      if (!changed) break;
    }

    var maxLayer = 0;
    nodes.forEach(function (n) { maxLayer = Math.max(maxLayer, map[n.id].layer); });
    var layers = [];
    for (var L = 0; L <= maxLayer; L++) layers[L] = [];
    nodes.forEach(function (n) { layers[map[n.id].layer].push(n); });

    // 尺寸
    var baseSize = spec.font_size || 30;
    var perRow = 0;
    layers.forEach(function (l) { perRow = Math.max(perRow, l.length); });
    var gapX = 40;
    var usableW = box.w;
    var nodeW = clamp((usableW - gapX * (perRow - 1)) / Math.max(perRow, 1), 140, perRow > 2 ? 320 : 440);

    var metrics = {};
    nodes.forEach(function (n) {
      var padX = 30, padY = 24;
      var innerW = nodeW - padX * 2;
      var size = n.size || baseSize;
      var lines = wrapText(n.text || '', innerW, size, 600);
      if (lines.length > 4) {
        size = Math.max(size * 0.82, 20);
        lines = wrapText(n.text || '', innerW, size, 600);
      }
      var tw = 0;
      lines.forEach(function (l) { tw = Math.max(tw, textW(l, size, 600)); });
      var w = clamp(tw + padX * 2, 140, nodeW);
      var h = lines.length * size * 1.42 + padY * 2;
      if (n.shape === 'diamond') { w = Math.max(w * 1.16, 200); h = h * 1.5 + 10; }
      if (n.shape === 'pill') h = Math.max(h, 88);
      metrics[n.id] = { w: w, h: h, lines: lines, size: size, padX: padX, padY: padY };
    });

    // 垂直排布：层间距自适应撑满画布，避免内容缩在中间一小条
    var layerH = layers.map(function (l) {
      return l.reduce(function (m, n) { return Math.max(m, metrics[n.id].h); }, 0);
    });
    var sumLayerH = layerH.reduce(function (a, b) { return a + b; }, 0);
    var gapY = spec.gap_y || clamp((box.h - sumLayerH) / Math.max(maxLayer, 1), 56, 200);
    var totalH = sumLayerH + gapY * maxLayer;
    var startY = box.y + Math.max(0, (box.h - totalH) / 2);
    var layerY = [];
    var acc = startY;
    for (var k = 0; k <= maxLayer; k++) {
      layerY[k] = acc + layerH[k] / 2;
      acc += layerH[k] + gapY;
    }

    var pos = {};
    layers.forEach(function (l, li) {
      var totalW = l.reduce(function (s, n) { return s + metrics[n.id].w; }, 0) + gapX * (l.length - 1);
      var x = box.x + (box.w - totalW) / 2;
      l.forEach(function (n) {
        var m = metrics[n.id];
        pos[n.id] = { x: x + m.w / 2, y: layerY[li], w: m.w, h: m.h };
        x += m.w + gapX;
      });
    });

    // 画边（先画，压在节点下）
    var reverseBuf = [];
    edges.forEach(function (ed) {
      var a = pos[ed.from], b = pos[ed.to];
      var isBack = map[ed.to].layer <= map[ed.from].layer;
      var gg = el('g');
      g.appendChild(gg);
      if (isBack) {
        reverseBuf.push({ ed: ed, a: a, b: b, gg: gg });
      } else {
        var sy = a.y + a.h / 2, ty = b.y - b.h / 2;
        var sameLayer = Math.abs(a.y - b.y) < 1;
        var p;
        if (sameLayer) {
          p = connectH(gg, a.x + a.w / 2, a.y, b.x - b.w / 2, b.y, T.a3, { sw: 3, dash: '9 8' });
        } else {
          p = connectV(gg, a.x, sy, b.x, ty, ed.style === 'dashed' ? T.a4 : T.a3, {
            sw: 3.2, dash: ed.style === 'dashed' ? '10 9' : null
          });
        }
        reg(ed.id || (ed.from + '_' + ed.to), gg, { kind: 'stroke', owner: ed.to });
        if (ed.label) {
          var lx = sameLayer ? (a.x + b.x) / 2 : (a.x * 0.42 + b.x * 0.58);
          var ly = sameLayer ? a.y - 30 : (sy + ty) / 2;
          var lg = el('g');
          labelChip(lg, ed.label, lx, ly, {
            size: Math.max(20, baseSize * 0.7), bg: T.bg, fill: T.sub, stroke: T.cardStroke, sw: 1.6
          });
          gg.appendChild(lg);
        }
      }
    });

    // 反向边（左侧回绕）
    reverseBuf.forEach(function (r) {
      var xOut = Math.min(r.a.x, r.b.x) - Math.min(r.a.w, r.b.w) / 2 - 46;
      var d = 'M' + (r.a.x - r.a.w / 2) + ',' + r.a.y +
        ' C' + (xOut - 10) + ',' + r.a.y + ' ' + (xOut - 10) + ',' + r.b.y +
        ' ' + (r.b.x - r.b.w / 2) + ',' + r.b.y;
      path(r.gg, { d: d, stroke: T.a2, sw: 2.8, dash: '9 8', markerEnd: 'url(#' + ensureMarker(T.a2) + ')' });
      reg(r.ed.id || (r.ed.from + '_' + r.ed.to), r.gg, { kind: 'stroke', owner: r.ed.to });
    });

    // 画节点
    nodes.forEach(function (n) {
      var m = metrics[n.id], p = pos[n.id];
      var gg = el('g');
      g.appendChild(gg);
      var isNote = n.shape === 'note';
      var shapeEls = [];
      if (n.shape === 'diamond') {
        var hw = p.w / 2, hh = p.h / 2;
        var d = 'M' + p.x + ',' + (p.y - hh) + ' L' + (p.x + hw) + ',' + p.y +
          ' L' + p.x + ',' + (p.y + hh) + ' L' + (p.x - hw) + ',' + p.y + ' Z';
        shapeEls.push(path(gg, {
          d: d, fill: n.fill || T.soft, stroke: n.stroke || T.a1, sw: 3
        }));
        textBlock(gg, m.lines, p.x, p.y - (m.lines.length * m.size * 1.42) / 2, {
          size: m.size, weight: 600, fill: T.ink, anchor: 'middle'
        });
      } else if (n.shape === 'pill') {
        rect(gg, {
          x: p.x - p.w / 2, y: p.y - p.h / 2, w: p.w, h: p.h, r: p.h / 2,
          fill: n.fill || T.a1, stroke: 'none'
        });
        textBlock(gg, m.lines, p.x, p.y - (m.lines.length * m.size * 1.42) / 2, {
          size: m.size, weight: 700, fill: '#fff', anchor: 'middle'
        });
      } else {
        var fill = n.fill || (isNote ? T.note : T.card);
        var stroke = n.stroke || (isNote ? T.a4 : T.cardStroke);
        rect(gg, {
          x: p.x - p.w / 2, y: p.y - p.h / 2, w: p.w, h: p.h, r: isNote ? 8 : 16,
          fill: fill, stroke: stroke, sw: isNote ? 2.4 : 2.4,
          dash: isNote ? '11 8' : null
        });
        textBlock(gg, m.lines, p.x, p.y - (m.lines.length * m.size * 1.42) / 2, {
          size: m.size, weight: isNote ? 500 : 600, fill: isNote ? T.sub : T.ink, anchor: 'middle'
        });
      }
      reg(n.id, gg, { kind: 'fade' });
    });
  }

  /* ============================ 图型：concept ============================ */

  function layoutConcept(spec, box) {
    var T = S.T, g = S.root;
    var center = spec.center || { text: spec.title || '核心概念' };
    var branches = spec.branches || [];
    var vertical = S.H > S.W;
    var baseSize = spec.font_size || 30;

    if (vertical) {
      var cw = Math.min(box.w * 0.78, 620);
      var cSize = baseSize + 12;
      var cLines = wrapText(center.text || '', cw - 60, cSize, 800);
      var ch = cLines.length * cSize * 1.42 + 56;
      var cx = box.x + box.w / 2, cy = box.y + ch / 2;

      var cg = el('g'); g.appendChild(cg);
      rect(cg, {
        x: cx - cw / 2, y: cy - ch / 2, w: cw, h: ch, r: 24,
        fill: T.a1, stroke: 'none'
      });
      textBlock(cg, cLines, cx, cy - (cLines.length * cSize * 1.42) / 2, {
        size: cSize, weight: 800, fill: '#fff', anchor: 'middle'
      });
      reg(center.id || 'center', cg, { kind: 'fade' });

      var spineX = box.x + 46;
      var n0 = Math.max(branches.length, 1);
      var top = cy + ch / 2 + 34;                 // 分支区顶边
      var availH = Math.max(160, box.y + box.h - top);
      // 分支字号随可用高度自适应：画布大就放大，避免卡片小而散
      var bSize = clamp(baseSize * Math.min(1.7, Math.sqrt(availH / (n0 * 230))), baseSize * 0.92, baseSize * 1.7);
      var bx = spineX + 34;
      var bw = box.x + box.w - bx;

      // 主干占位：先入 DOM，保证压在卡片下层
      var spineG = el('g'); g.appendChild(spineG);

      // 第一遍：按内容算每张卡片的自然高度
      var cards = branches.map(function (br) {
        var kids = br.children || [];
        var kidLines = kids.map(function (k) {
          return { id: k.id, lines: wrapText('· ' + (k.text || ''), bw - 76, bSize - 8, 400) };
        });
        var titleLines = wrapText(br.text || '', bw - 60, bSize, 700);
        var rowH = titleLines.length * bSize * 1.42;
        var kidH = 0;
        kidLines.forEach(function (k) { kidH += k.lines.length * (bSize - 8) * 1.46; });
        return {
          br: br, kids: kidLines, titleLines: titleLines, rowH: rowH,
          h: Math.max(104, rowH + (kidH ? kidH + 12 : 0) + 40),
          y: 0
        };
      });

      var sumH = 0;
      cards.forEach(function (c) { sumH += c.h; });
      var gaps = n0 > 1 ? clamp((availH - sumH) / (n0 - 1), 18, 240) : 0;

      // 第二遍：顺序排布
      var curY = top;
      cards.forEach(function (c, i) {
        var y = curY + c.h / 2;
        curY += c.h + gaps;
        c.y = y;
        var br = c.br;
        var bg = el('g'); g.appendChild(bg);

        rect(bg, {
          x: bx, y: y - c.h / 2, w: bw, h: c.h, r: 16,
          fill: br.fill || T.card, stroke: br.stroke || T.cardStroke, sw: 2
        });
        rect(bg, {
          x: bx, y: y - c.h / 2, w: 8, h: c.h, r: 4,
          fill: BRANCH_COLORS[i % BRANCH_COLORS.length](T)
        });

        var ty = y - c.h / 2 + 20;
        textBlock(bg, c.titleLines, bx + 34, ty, { size: bSize, weight: 700, fill: T.ink, maxW: bw - 60 });
        if (c.kids.length) {
          var ky = ty + c.rowH + 8;
          c.kids.forEach(function (k) {
            var kg = el('g'); bg.appendChild(kg);
            textBlock(kg, k.lines, bx + 34, ky, { size: bSize - 8, weight: 400, fill: T.sub, maxW: bw - 76 });
            if (k.id) reg(k.id, kg, { kind: 'fade', owner: br.id });
            ky += k.lines.length * (bSize - 8) * 1.46;
          });
        }
        var lg = el('g'); g.appendChild(lg);
        elbow(lg, [[spineX, y], [bx - 2, y]], T.a1, { sw: 2.6, arrow: true });
        reg(br.id + '__link', lg, { kind: 'stroke', owner: br.id });
        reg(br.id, bg, { kind: 'fade' });
      });

      // 主干：从中心卡片弯下来，串起所有分支
      if (cards.length) {
        var firstY = cards[0].y;
        var lastY = cards[cards.length - 1].y;
        path(spineG, {
          d: 'M' + (cx - cw / 2) + ',' + (cy + ch / 2) + ' C' + (cx - cw / 2) + ',' + (cy + ch / 2 + 44) + ' ' + spineX + ',' + (firstY - 34) + ' ' + spineX + ',' + firstY,
          stroke: T.a1, sw: 3, dash: '10 9', opacity: 0.55
        });
        path(spineG, { d: 'M' + spineX + ',' + firstY + ' L' + spineX + ',' + lastY, stroke: T.a1, sw: 3, dash: '10 9', opacity: 0.55 });
        reg('_spine', spineG, { kind: 'stroke', owner: branches[0].id });
      }
    } else {
      // 横屏：中心在左，分支在右
      var cw2 = Math.min(box.w * 0.30, 460);
      var cSize2 = baseSize + 10;
      var cLines2 = wrapText(center.text || '', cw2 - 56, cSize2, 800);
      var ch2 = Math.max(cLines2.length * cSize2 * 1.42 + 64, 260);
      var cx2 = box.x + cw2 / 2, cy2 = box.y + box.h / 2;

      var cg2 = el('g'); g.appendChild(cg2);
      rect(cg2, { x: cx2 - cw2 / 2, y: cy2 - ch2 / 2, w: cw2, h: ch2, r: 26, fill: T.a1 });
      textBlock(cg2, cLines2, cx2, cy2 - (cLines2.length * cSize2 * 1.42) / 2, {
        size: cSize2, weight: 800, fill: '#fff', anchor: 'middle'
      });
      reg(center.id || 'center', cg2, { kind: 'fade' });

      var bx0 = box.x + cw2 + 110;
      var bw2 = box.x + box.w - bx0;
      var n = branches.length || 1;
      var bh2 = clamp((box.h - 24 * (n - 1)) / n, 92, 210);
      var startY2 = box.y + (box.h - (bh2 * n + 24 * (n - 1))) / 2;
      var bSize2 = baseSize - 2;

      branches.forEach(function (br, i) {
        var y2 = startY2 + i * (bh2 + 24) + bh2 / 2;
        var bg2 = el('g'); g.appendChild(bg2);
        var kids = br.children || [];
        var kidText = kids.map(function (k) { return k.text; }).filter(Boolean).join('  ·  ');
        var titleLines = wrapText(br.text || '', bw2 - 56, bSize2, 700);
        var kidLines2 = kidText ? wrapText(kidText, bw2 - 60, bSize2 - 9, 400) : [];
        var needH2 = titleLines.length * bSize2 * 1.42 + (kidLines2.length ? kidLines2.length * (bSize2 - 9) * 1.46 + 8 : 0) + 36;
        var hh = Math.max(bh2, needH2);

        rect(bg2, {
          x: bx0, y: y2 - hh / 2, w: bw2, h: hh, r: 16,
          fill: br.fill || T.card, stroke: br.stroke || T.cardStroke, sw: 2
        });
        rect(bg2, { x: bx0, y: y2 - hh / 2, w: 8, h: hh, r: 4, fill: BRANCH_COLORS[i % BRANCH_COLORS.length](T) });

        var ty2 = y2 - hh / 2 + 20;
        textBlock(bg2, titleLines, bx0 + 32, ty2, { size: bSize2, weight: 700, fill: T.ink, maxW: bw2 - 56 });
        if (kidLines2.length) {
          textBlock(bg2, kidLines2, bx0 + 32, ty2 + titleLines.length * bSize2 * 1.42 + 4, {
            size: bSize2 - 9, weight: 400, fill: T.sub, maxW: bw2 - 60
          });
        }
        var lg2 = el('g'); g.appendChild(lg2);
        elbow(lg2, [[cx2 + cw2 / 2, cy2], [cx2 + cw2 / 2 + 56, cy2], [cx2 + cw2 / 2 + 56, y2], [bx0 - 2, y2]], T.a1, { sw: 2.6 });
        reg(br.id + '__link', lg2, { kind: 'stroke', owner: br.id });
        reg(br.id, bg2, { kind: 'fade' });
      });
    }
  }

  var BRANCH_COLORS = [
    function (T) { return T.a1; },
    function (T) { return T.a3; },
    function (T) { return T.a4; },
    function (T) { return T.a2; }
  ];

  /* ============================ 图型：timeline ============================ */

  function layoutTimeline(spec, box) {
    var T = S.T, g = S.root;
    var events = spec.events || [];
    if (!events.length) return;
    var vertical = S.H > S.W;
    var baseSize = spec.font_size || 30;

    if (vertical) {
      var axisX = box.x + 54;
      var cardX = axisX + 66;
      var cardW = box.x + box.w - cardX;
      var n = events.length;
      var slot = (box.h - 10) / n;
      var size = Math.min(baseSize, 30);

      var axisG = el('g'); g.appendChild(axisG);
      path(axisG, {
        d: 'M' + axisX + ',' + (box.y + 8) + ' L' + axisX + ',' + (box.y + box.h - 8),
        stroke: T.line, sw: 3
      });
      reg('_axis', axisG, { kind: 'stroke', owner: events[0].id });

      events.forEach(function (ev, i) {
        var cy = box.y + slot * i + slot / 2;
        var eg = el('g'); g.appendChild(eg);

        // 轴点
        circle(eg, { cx: axisX, cy: cy, r: 11, fill: T.bg, stroke: T.a1, sw: 4 });
        circle(eg, { cx: axisX, cy: cy, r: 4, fill: T.a1 });

        var titleLines = wrapText(ev.text || '', cardW - 40, size, 600);
        var detailLines = ev.detail ? wrapText(ev.detail, cardW - 40, size - 8, 400) : [];
        var dateSize = size + 2;
        var dh = dateSize * 1.4 + 6;
        var th = titleLines.length * size * 1.42;
        var dd = detailLines.length ? detailLines.length * (size - 8) * 1.42 + 4 : 0;
        var blockH = dh + th + dd;
        var top = cy - blockH / 2;

        if (ev.date) {
          textBlock(eg, ev.date, cardX, top, { size: dateSize, weight: 800, fill: T.a1, maxW: cardW - 40 });
        }
        textBlock(eg, titleLines, cardX, top + dh, { size: size, weight: 600, fill: T.ink, maxW: cardW - 40 });
        if (detailLines.length) {
          textBlock(eg, detailLines, cardX, top + dh + th + 3, { size: size - 8, weight: 400, fill: T.sub, maxW: cardW - 40 });
        }
        reg(ev.id, eg, { kind: 'fade' });
      });
    } else {
      var axisY = box.y + box.h * 0.52;
      var n2 = events.length;
      var slotX = box.w / n2;
      var cardW2 = Math.min(slotX - 40, 420);
      var size2 = Math.min(baseSize, 30);

      var axisG2 = el('g'); g.appendChild(axisG2);
      path(axisG2, { d: 'M' + (box.x + 8) + ',' + axisY + ' L' + (box.x + box.w - 8) + ',' + axisY, stroke: T.line, sw: 3 });
      reg('_axis', axisG2, { kind: 'stroke', owner: events[0].id });

      events.forEach(function (ev, i) {
        var cx = box.x + slotX * i + slotX / 2;
        var up = i % 2 === 0;
        var eg = el('g'); g.appendChild(eg);

        circle(eg, { cx: cx, cy: axisY, r: 10, fill: T.bg, stroke: T.a1, sw: 4 });

        var titleLines = wrapText(ev.text || '', cardW2, size2, 600);
        var detailLines = ev.detail ? wrapText(ev.detail, cardW2, size2 - 8, 400) : [];
        var blockH = titleLines.length * size2 * 1.42 + (detailLines.length ? detailLines.length * (size2 - 8) * 1.42 + 4 : 0) + 34;

        path(eg, {
          d: 'M' + cx + ',' + axisY + ' L' + cx + ',' + (up ? axisY - 34 : axisY + 34),
          stroke: T.line, sw: 2.4, dash: '6 6'
        });

        var top = up ? axisY - 34 - blockH : axisY + 34;
        if (ev.date) {
          textBlock(eg, ev.date, cx, top, { size: size2 + 2, weight: 800, fill: T.a1, anchor: 'middle', maxW: cardW2 });
        }
        textBlock(eg, titleLines, cx, top + (size2 + 2) * 1.4 + 6, { size: size2, weight: 600, fill: T.ink, anchor: 'middle', maxW: cardW2 });
        if (detailLines.length) {
          textBlock(eg, detailLines, cx, top + (size2 + 2) * 1.4 + 6 + titleLines.length * size2 * 1.42 + 3, {
            size: size2 - 8, weight: 400, fill: T.sub, anchor: 'middle', maxW: cardW2
          });
        }
        reg(ev.id, eg, { kind: 'fade' });
      });
    }
  }

  /* ============================ 图型：compare ============================ */

  function layoutCompare(spec, box) {
    var T = S.T, g = S.root;
    var L = spec.left || {}, R = spec.right || {};
    var vertical = S.H > S.W;
    var baseSize = spec.font_size || 30;
    var headSize = baseSize + 6;
    var itemSize = baseSize - 2;

    function panel(px, py, pw, ph, side, theme) {
      var pg = el('g'); g.appendChild(pg);
      rect(pg, { x: px, y: py, w: pw, h: ph, r: 20, fill: side.fill || T.card, stroke: side.stroke || T.cardStroke, sw: 2.4 });
      // 标题条
      rect(pg, { x: px, y: py, w: pw, h: 74, r: 20, fill: side.accent || (side === L ? T.a1 : T.a2) });
      rect(pg, { x: px, y: py + 54, w: pw, h: 20, fill: side.accent || (side === L ? T.a1 : T.a2) });
      textBlock(pg, side.title || '', px + pw / 2, py + (74 - headSize * 1.42) / 2, {
        size: headSize, weight: 800, fill: '#fff', anchor: 'middle', maxW: pw - 40
      });
      return pg;
    }

    var panes = [];
    var pw, ph, lx, ly, rx, ry;
    var gapMid = vertical ? 100 : 130;
    // 有结论条时预留高度，避免溢出画布
    var concH = (spec.conclusion && spec.conclusion.text) ? 124 : 0;

    if (vertical) {
      pw = box.w;
      ph = (box.h - gapMid - concH) / 2 - 34;
      lx = box.x; ly = box.y;
      rx = box.x; ry = box.y + ph + gapMid;
    } else {
      pw = (box.w - gapMid) / 2;
      ph = box.h - 30 - concH;
      lx = box.x; ly = box.y;
      rx = box.x + pw + gapMid; ry = box.y;
    }

    var lg = panel(lx, ly, pw, ph, L);
    var rg = panel(rx, ry, pw, ph, R);
    reg(L.id || 'left', lg, { kind: 'fade' });
    reg(R.id || 'right', rg, { kind: 'fade' });

    // 条目
    var itemsL = L.items || [], itemsR = R.items || [];
    var rows = Math.max(itemsL.length, itemsR.length);
    var rowH = Math.min(170, (ph - 104) / Math.max(rows, 1));

    function drawItems(pg, px, py, side, items, accentColor) {
      items.forEach(function (it, i) {
        var y = py + 96 + rowH * i + rowH / 2;
        var ig = el('g'); pg.appendChild(ig);
        circle(ig, { cx: px + 34, cy: y - itemSize * 0.42, r: 6, fill: accentColor });
        var lines = wrapText(it.text || '', pw - 96, itemSize, 500);
        textBlock(ig, lines, px + 52, y - (lines.length * itemSize * 1.42) / 2, {
          size: itemSize, weight: 500, fill: T.ink, maxW: pw - 96
        });
        if (i < items.length - 1) {
          path(ig, {
            d: 'M' + (px + 30) + ',' + (y + rowH / 2 - 2) + ' L' + (px + pw - 30) + ',' + (y + rowH / 2 - 2),
            stroke: T.cardStroke, sw: 1.4, dash: '4 6'
          });
        }
        reg(it.id, ig, { kind: 'fade', owner: side.id || null });
      });
    }

    drawItems(lg, lx, ly, L, itemsL, L.accent || T.a1);
    drawItems(rg, rx, ry, R, itemsR, R.accent || T.a2);

    // VS 徽标
    var vg = el('g'); g.appendChild(vg);
    var vcx = vertical ? box.x + box.w / 2 : lx + pw + gapMid / 2;
    var vcy = vertical ? ly + ph + gapMid / 2 + 4 : ly + ph * 0.42;
    circle(vg, { cx: vcx, cy: vcy, r: vertical ? 28 : 34, fill: T.ink });
    textBlock(vg, 'VS', vcx, vcy - (vertical ? 12 : 15), { size: vertical ? 22 : 26, weight: 800, fill: T.bg, anchor: 'middle' });
    reg('_vs', vg, { kind: 'fade', owner: R.id || 'right' });

    if (spec.conclusion && spec.conclusion.text) {
      var cg = el('g'); g.appendChild(cg);
      var cy2 = box.y + box.h + 24;
      var hl = wrapText(spec.conclusion.text, box.w - 80, baseSize, 700);
      var hh = hl.length * baseSize * 1.42 + 40;
      var yBase = vertical ? ly + ph * 2 + gapMid + 30 : box.y + ph + 34;
      rect(cg, { x: box.x, y: yBase, w: box.w, h: hh, r: 16, fill: T.a3, opacity: 0.14 });
      rect(cg, { x: box.x, y: yBase, w: 7, h: hh, r: 4, fill: T.a3 });
      textBlock(cg, hl, box.x + 30, yBase + 20, { size: baseSize, weight: 700, fill: T.ink, maxW: box.w - 70 });
      reg(spec.conclusion.id || '_conclusion', cg, { kind: 'fade' });
    }
  }

  /* ============================ 图型：curve ============================ */

  function niceTicks(min, max, count) {
    var span = max - min;
    if (span <= 0) return [min];
    var step = Math.pow(10, Math.floor(Math.log(span / count) / Math.LN10));
    var err = span / count / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var out = [];
    for (var v = Math.ceil(min / step) * step; v <= max + step * 0.001; v += step) {
      out.push(Math.round(v * 1e6) / 1e6);
    }
    return out;
  }

  function catmullRom(pts) {
    if (pts.length < 2) return '';
    if (pts.length === 2) return 'M' + pts[0][0] + ',' + pts[0][1] + ' L' + pts[1][0] + ',' + pts[1][1];
    var d = 'M' + pts[0][0] + ',' + pts[0][1];
    for (var i = 0; i < pts.length - 1; i++) {
      var p0 = pts[i - 1] || pts[i];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = pts[i + 2] || p2;
      var c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
      var c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ' C' + c1x + ',' + c1y + ' ' + c2x + ',' + c2y + ' ' + p2[0] + ',' + p2[1];
    }
    return d;
  }

  function polyD(pts) {
    var d = 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
    for (var i = 1; i < pts.length; i++) d += ' L' + pts[i][0].toFixed(1) + ',' + pts[i][1].toFixed(1);
    return d;
  }

  /* ---------- 函数表达式求值（自建解析器，不用 eval / new Function） ---------- */

  var EXPR_FUNCS = {
    sin: Math.sin, cos: Math.cos, tan: Math.tan,
    asin: Math.asin, acos: Math.acos, atan: Math.atan,
    sqrt: Math.sqrt, abs: Math.abs, exp: Math.exp,
    ln: Math.log, log: function (v) { return Math.log(v) / Math.LN10; },
    floor: Math.floor, ceil: Math.ceil, round: Math.round
  };
  var EXPR_CONST = { pi: Math.PI, 'π': Math.PI, e: Math.E };

  function compileExpr(src) {
    var s = String(src == null ? '' : src).replace(/\s+/g, '');
    var i = 0;

    function peek() { return s.charAt(i); }
    function eat(ch) { if (s.charAt(i) === ch) { i++; return true; } return false; }
    function isAlpha(ch) { return /[a-zA-Z\u03b1-\u03c9\u0391-\u03a9]/.test(ch); }
    function isDigit(ch) { return /[0-9.]/.test(ch); }

    function parseNumber() {
      var st = i;
      while (isDigit(s.charAt(i))) i++;
      var v = parseFloat(s.slice(st, i));
      if (!isFinite(v)) throw new Error('数字格式错误："' + src + '"');
      return { t: 'num', v: v };
    }
    function parseIdent() {
      var st = i;
      while (isAlpha(s.charAt(i))) i++;
      return s.slice(st, i);
    }

    function parsePrimary() {
      if (eat('(')) {
        var e = parseExpr();
        eat(')');
        return e;
      }
      if (eat('-')) return { t: 'neg', a: parsePrimary() };
      if (eat('+')) return parsePrimary();
      if (isDigit(peek())) return parseNumber();
      if (isAlpha(peek())) {
        var id = parseIdent();
        if (id === 'x') return { t: 'x' };
        if (EXPR_CONST[id] != null) return { t: 'num', v: EXPR_CONST[id] };
        if (EXPR_FUNCS[id]) {
          if (!eat('(')) throw new Error('函数 "' + id + '" 后面要跟括号，例如 ' + id + '(x)');
          var arg = parseExpr();
          eat(')');
          return { t: 'call', f: id, a: arg };
        }
        throw new Error('表达式里有未知符号："' + id + '"（可用 x / pi / sin / cos / tan / sqrt / abs / ln / exp）');
      }
      throw new Error('表达式无法解析："' + src + '"（第 ' + (i + 1) + ' 个字符附近）');
    }

    function parsePower() {
      var base = parsePrimary();
      if (eat('^')) return { t: 'bin', op: '^', l: base, r: parseUnary() };
      return base;
    }
    function parseUnary() {
      if (eat('-')) return { t: 'neg', a: parseUnary() };
      if (eat('+')) return parseUnary();
      return parsePower();
    }
    function needMul() {
      var c = peek();
      return c === '(' || isAlpha(c);
    }
    function parseTerm() {
      var left = parseUnary();
      for (;;) {
        if (eat('*')) { left = { t: 'bin', op: '*', l: left, r: parseUnary() }; }
        else if (eat('/')) { left = { t: 'bin', op: '/', l: left, r: parseUnary() }; }
        else if (needMul()) { left = { t: 'bin', op: '*', l: left, r: parseUnary() }; }
        else break;
      }
      return left;
    }
    function parseExpr() {
      var left = parseTerm();
      for (;;) {
        if (eat('+')) left = { t: 'bin', op: '+', l: left, r: parseTerm() };
        else if (eat('-')) left = { t: 'bin', op: '-', l: left, r: parseTerm() };
        else break;
      }
      return left;
    }

    var ast = parseExpr();
    if (i < s.length) throw new Error('表达式有多余字符："' + s.slice(i) + '"');

    function ev(n, x) {
      switch (n.t) {
        case 'num': return n.v;
        case 'x': return x;
        case 'neg': return -ev(n.a, x);
        case 'call': return EXPR_FUNCS[n.f](ev(n.a, x));
        case 'bin':
          var a = ev(n.l, x), b = ev(n.r, x);
          if (n.op === '+') return a + b;
          if (n.op === '-') return a - b;
          if (n.op === '*') return a * b;
          if (n.op === '/') return a / b;
          return Math.pow(a, b);
        default: return NaN;
      }
    }
    return function (x) { return ev(ast, x); };
  }

  function gcd(a, b) { return b ? gcd(b, a % b) : a; }

  // 把「π 的倍数」格式化成 π/2、3π/2、2π 这类刻度
  function fmtPiMul(v) {
    if (Math.abs(v) < 1e-9) return '0';
    var sign = v < 0 ? '-' : '';
    var a = Math.abs(v);
    var dens = [1, 2, 3, 4, 6];
    for (var k = 0; k < dens.length; k++) {
      var num = a * dens[k];
      if (Math.abs(num - Math.round(num)) < 1e-6) {
        var n = Math.round(num), d = dens[k];
        var gg = gcd(n, d) || 1;
        n /= gg; d /= gg;
        if (d === 1) return sign + (n === 1 ? '' : n) + 'π';
        return sign + (n === 1 ? '' : n) + 'π/' + d;
      }
    }
    return sign + (Math.round(a * 100) / 100) + 'π';
  }

  function layoutCurve(spec, box) {
    var T = S.T, g = S.root;
    // 曲线按画布精确布局，且溢出部分已用 clipPath 裁剪；
    // 若参与自动缩放，getBBox 会把裁剪外的范围算进来，导致整图被缩小
    S.noAutoScale = true;
    var ax = spec.axes || {};
    var xa = ax.x || { label: 'x', min: 0, max: 10 };
    var ya = ax.y || { label: 'y', min: 0, max: 10 };
    var cross = ax.style === 'cross';
    var xPi = xa.unit === 'pi';
    var baseSize = spec.font_size || 28;
    var tickSize = Math.max(18, baseSize * 0.7);
    var firstCurve = (spec.curves || [])[0];

    var padL = 96, padR = 60, padT = 48, padB = 96;
    var px0 = box.x + padL, py0 = box.y + padT;
    var pw = box.w - padL - padR, phh = box.h - padT - padB;
    var x1 = px0 + pw, y1 = py0 + phh;

    function sx(v) { return px0 + (v - xa.min) / (xa.max - xa.min) * pw; }
    function sy(v) { return y1 - (v - ya.min) / (ya.max - ya.min) * phh; }

    var xAxisY = cross ? sy(clamp(0, ya.min, ya.max)) : y1;
    var yAxisX = cross ? sx(clamp(0, xa.min, xa.max)) : px0;

    // 绘图区裁剪：超出范围的曲线直接出画面，而不是被压成水平线
    var clipId = 'curveClip';
    if (!S.defs.querySelector('#' + clipId)) {
      var cp = el('clipPath', { id: clipId });
      cp.appendChild(el('rect', { x: px0, y: py0, width: pw, height: phh }));
      S.defs.appendChild(cp);
    }
    function clipTo(gNode) { gNode.setAttribute('clip-path', 'url(#' + clipId + ')'); }

    // ---- 刻度 ----
    var yTicks = ya.ticks || niceTicks(ya.min, ya.max, spec.grid_y || 5);
    var xTicks;
    if (xa.ticks) {
      xTicks = xa.ticks;
    } else if (xPi) {
      var spanPi = xa.max - xa.min;
      var stepPi = spanPi > 8 ? 2 : (spanPi > 4 ? 1 : 0.5);
      xTicks = [];
      for (var tv = Math.ceil(xa.min / stepPi) * stepPi; tv <= xa.max + 1e-9; tv += stepPi) {
        xTicks.push(Math.round(tv * 1e6) / 1e6);
      }
    } else {
      xTicks = niceTicks(xa.min, xa.max, spec.grid_x || 6);
    }

    // ---- 网格与刻度标签 ----
    var gridG = el('g'); g.appendChild(gridG);
    var showGrid = ax.grid !== false;
    yTicks.forEach(function (t) {
      if (t < ya.min - 1e-9 || t > ya.max + 1e-9) return;
      if (showGrid) {
        path(gridG, {
          d: 'M' + px0 + ',' + sy(t) + ' L' + x1 + ',' + sy(t),
          stroke: T.cardStroke, sw: 1.2, dash: '3 7', opacity: cross ? 0.55 : 1
        });
      }
      if (cross && Math.abs(t) < 1e-9) return;   // 原点不标
      textBlock(gridG, String(t), yAxisX - 18, sy(t) - tickSize * 0.7, {
        size: tickSize, weight: 500, fill: T.faint, anchor: 'end'
      });
    });
    xTicks.forEach(function (t) {
      if (t < xa.min - 1e-9 || t > xa.max + 1e-9) return;
      if (showGrid) {
        path(gridG, {
          d: 'M' + sx(t) + ',' + py0 + ' L' + sx(t) + ',' + y1,
          stroke: T.cardStroke, sw: 1.2, dash: '3 7', opacity: cross ? 0.55 : 1
        });
      }
      if (cross && Math.abs(t) < 1e-9) return;
      textBlock(gridG, xPi ? fmtPiMul(t) : String(t), sx(t), xAxisY + 16, {
        size: tickSize, weight: 500, fill: T.faint, anchor: 'middle'
      });
    });
    reg('_grid', gridG, { kind: 'fade', owner: firstCurve && firstCurve.id });

    // 原点 O
    if (cross) {
      var og = el('g'); gridG.appendChild(og);
      textBlock(og, 'O', yAxisX - 14, xAxisY + 8, { size: tickSize, weight: 600, fill: T.sub, anchor: 'end' });
    }

    // ---- 区域色带 ----
    (spec.regions || []).forEach(function (rg) {
      var rgG = el('g'); g.appendChild(rgG);
      var rx = sx(rg.from), rw = sx(rg.to) - sx(rg.from);
      rect(rgG, { x: rx, y: py0, w: rw, h: phh, r: 4, fill: rg.color || T.a4, opacity: 0.16 });
      if (rg.label) {
        labelChip(rgG, rg.label, rx + rw / 2, py0 + 26, {
          size: tickSize, bg: T.bg, fill: T.sub, stroke: T.cardStroke, sw: 1.4, anchor: 'middle'
        });
      }
      reg(rg.id || ('_region' + rg.from), rgG, { kind: 'fade' });
    });

    // ---- 坐标轴 ----
    var axisG = el('g'); g.appendChild(axisG);
    if (cross) {
      var axCol = T.ink;
      path(axisG, {
        d: 'M' + px0 + ',' + xAxisY + ' L' + (x1 + 14) + ',' + xAxisY,
        stroke: axCol, sw: 2.6, cap: 'round',
        markerEnd: 'url(#' + ensureMarker(axCol) + ')'
      });
      path(axisG, {
        d: 'M' + yAxisX + ',' + y1 + ' L' + yAxisX + ',' + (py0 - 14),
        stroke: axCol, sw: 2.6, cap: 'round',
        markerEnd: 'url(#' + ensureMarker(axCol) + ')'
      });
    } else {
      path(axisG, { d: 'M' + px0 + ',' + py0 + ' L' + px0 + ',' + y1 + ' L' + x1 + ',' + y1, stroke: T.ink, sw: 3, cap: 'square' });
    }
    reg('_axes', axisG, { kind: 'stroke', owner: firstCurve && firstCurve.id });

    if (xa.label) {
      if (cross) {
        textBlock(axisG, xa.label, x1 - 4, xAxisY - baseSize * 1.85, { size: baseSize, weight: 700, fill: T.sub, anchor: 'end' });
      } else {
        textBlock(axisG, xa.label, (px0 + x1) / 2, y1 + 54, { size: baseSize, weight: 600, fill: T.sub, anchor: 'middle' });
      }
    }
    if (ya.label) {
      if (cross) {
        textBlock(axisG, ya.label, yAxisX + 12, py0 - baseSize * 1.55, { size: baseSize, weight: 700, fill: T.sub });
      } else {
        textBlock(axisG, ya.label, box.x + 4, py0 - baseSize * 1.6, { size: baseSize, weight: 700, fill: T.sub });
      }
    }

    // ---- 辅助线 ----
    (spec.guides || []).forEach(function (gd) {
      var gg2 = el('g'); g.appendChild(gg2);
      var col = gd.color || T.a4;
      if (gd.type === 'h') {
        var gy = sy(gd.at);
        path(gg2, { d: 'M' + px0 + ',' + gy + ' L' + x1 + ',' + gy, stroke: col, sw: 2, dash: '8 8', opacity: 0.8 });
        if (gd.label) {
          labelChip(gg2, gd.label, px0 + 10, gy - 20, { size: tickSize, bg: T.bg, fill: col, stroke: col, sw: 1.4, weight: 700 });
        }
      } else {
        var gx = sx(gd.at);
        path(gg2, { d: 'M' + gx + ',' + py0 + ' L' + gx + ',' + y1, stroke: col, sw: 2, dash: '8 8', opacity: 0.8 });
        if (gd.label) {
          labelChip(gg2, gd.label, gx, py0 - 6, { size: tickSize, bg: T.bg, fill: col, stroke: col, sw: 1.4, weight: 700, anchor: 'middle' });
        }
      }
      reg(gd.id, gg2, { kind: 'stroke' });
    });

    // ---- 曲线 ----
    var palette = [T.a1, T.a2, T.a3, T.a4];

    function sampleFn(cv) {
      var dom = cv.domain || [xa.min, xa.max];
      var n = cv.samples || 280;
      var span = ya.max - ya.min;
      var lo = ya.min - span * 0.6, hi = ya.max + span * 0.6;
      var fn;
      try { fn = compileExpr(cv.fn); }
      catch (err) { console.warn('[teach-diagram] 曲线 "' + cv.id + '" 表达式有误：' + err.message); return []; }

      var segs = [], curSeg = [], prevRaw = null;
      for (var k = 0; k <= n; k++) {
        var xv = dom[0] + (dom[1] - dom[0]) * (k / n);
        var real = xPi ? xv * Math.PI : xv;
        var yv = fn(real);
        if (!isFinite(yv)) {
          if (curSeg.length) { segs.push(curSeg); curSeg = []; }
          prevRaw = null;
          continue;
        }
        // 跨渐近线：正负号翻转且跳变过大 → 断开，避免画出竖线
        if (prevRaw !== null && Math.abs(yv - prevRaw) > span * 2.5 && (yv >= 0) !== (prevRaw >= 0)) {
          if (curSeg.length) { segs.push(curSeg); curSeg = []; }
        }
        curSeg.push([sx(xv), sy(clamp(yv, lo, hi))]);
        prevRaw = yv;
      }
      if (curSeg.length) segs.push(curSeg);
      return segs;
    }

    (spec.curves || []).forEach(function (cv, i) {
      var cg = el('g'); g.appendChild(cg);
      clipTo(cg);
      var color = cv.color || palette[i % palette.length];
      var fromFn = !!cv.fn;
      var segs = fromFn ? sampleFn(cv) : [(cv.points || []).map(function (p) { return [sx(p[0]), sy(p[1])]; })];
      var allPts = [];

      segs.forEach(function (pts) {
        if (!pts.length) return;
        allPts = allPts.concat(pts);
        if (pts.length < 2) return;
        path(cg, {
          d: pts.length > 60 ? polyD(pts) : catmullRom(pts),
          stroke: color, sw: cv.width || 4.4, dash: cv.dashed ? '12 9' : null
        });
      });

      // 端点小圆：只在手写 points 模式画（函数曲线两端是截断的，画上去反而怪）
      if (!fromFn && allPts.length) {
        circle(cg, { cx: allPts[0][0], cy: allPts[0][1], r: 6, fill: T.bg, stroke: color, sw: 3 });
        var e0 = allPts[allPts.length - 1];
        circle(cg, { cx: e0[0], cy: e0[1], r: 6, fill: T.bg, stroke: color, sw: 3 });
      }

      // 曲线名
      if (cv.label && allPts.length) {
        var lx, ly;
        if (cv.label_at) {
          lx = sx(cv.label_at[0]); ly = sy(cv.label_at[1]);
        } else if (fromFn) {
          var mp = allPts[Math.floor(allPts.length * 0.72)];
          lx = mp[0]; ly = mp[1] - 34;
        } else {
          var lp = allPts[allPts.length - 1];
          lx = clamp(lp[0], px0 + 80, x1 - 10); ly = lp[1] - 34;
        }
        labelChip(cg, cv.label, lx, clamp(ly, py0 + 18, y1 - 26), {
          size: tickSize + 2, bg: T.bg, fill: color, stroke: color, sw: 1.6, anchor: 'middle', weight: 700
        });
      }
      reg(cv.id, cg, { kind: 'stroke' });
    });

    // ---- 标记点 ----
    (spec.markers || []).forEach(function (mk) {
      var mg = el('g'); g.appendChild(mg);
      var mx = sx(mk.x), my = sy(mk.y);
      circle(mg, { cx: mx, cy: my, r: 9, fill: T.bg, stroke: mk.color || T.ink, sw: 4 });
      if (mk.text) {
        var right = mx < px0 + pw * 0.62;
        labelChip(mg, mk.text, mx + (right ? 22 : -22), my - 26, {
          size: tickSize, bg: T.ink, fill: T.bg, anchor: right ? 'start' : 'end', weight: 700
        });
      }
      reg(mk.id, mg, { kind: 'fade' });
    });
  }

  /* ============================ 图型：cycle ============================ */

  function layoutCycle(spec, box) {
    var T = S.T, g = S.root;
    var nodes = spec.nodes || [];
    if (!nodes.length) return;
    var baseSize = spec.font_size || 28;
    var n = nodes.length;

    var vertical = S.H > S.W;
    var cx = box.x + box.w / 2, cy = box.y + box.h / 2;
    var rx = vertical ? box.w * 0.30 : box.w * 0.28;
    var ry = vertical ? box.h * 0.29 : box.h * 0.32;

    var size = Math.min(baseSize, 28);
    var metrics = nodes.map(function (nd) {
      var lines = wrapText(nd.text || '', vertical ? 200 : 220, size, 600);
      var w = 0;
      lines.forEach(function (l) { w = Math.max(w, textW(l, size, 600)); });
      return { lines: lines, w: Math.max(w + 44, 120), h: lines.length * size * 1.42 + 34 };
    });

    var ang = nodes.map(function (nd, i) { return -Math.PI / 2 + (i / n) * Math.PI * 2; });
    var pos = nodes.map(function (nd, i) {
      return { x: cx + Math.cos(ang[i]) * rx, y: cy + Math.sin(ang[i]) * ry };
    });

    // 环形箭头
    var ringG = el('g'); g.appendChild(ringG);
    for (var i = 0; i < n; i++) {
      var a0 = ang[i] + 0.20;
      var a1 = ang[(i + 1) % n] + (i === n - 1 ? Math.PI * 2 - 0.20 : -0.20);
      if (i === n - 1) a1 = ang[0] + Math.PI * 2 - 0.20;
      var p0x = cx + Math.cos(a0) * rx, p0y = cy + Math.sin(a0) * ry;
      var p1x = cx + Math.cos(a1) * rx, p1y = cy + Math.sin(a1) * ry;
      var large = (a1 - a0) > Math.PI ? 1 : 0;
      var seg = el('g'); g.appendChild(seg);
      path(seg, {
        d: 'M' + p0x + ',' + p0y + ' A' + rx + ',' + ry + ' 0 ' + large + ' 1 ' + p1x + ',' + p1y,
        stroke: T.a1, sw: 2.8, dash: '9 9', opacity: 0.62,
        markerEnd: 'url(#' + ensureMarker(T.a1) + ')'
      });
      reg('_arc' + i, seg, { kind: 'stroke', owner: nodes[(i + 1) % n].id });
    }

    // 节点
    nodes.forEach(function (nd, i) {
      var m = metrics[i], p = pos[i];
      var ng = el('g'); g.appendChild(ng);
      var color = [T.a1, T.a3, T.a4, T.a2][i % 4];
      rect(ng, {
        x: p.x - m.w / 2, y: p.y - m.h / 2, w: m.w, h: m.h, r: 16,
        fill: T.card, stroke: color, sw: 2.6
      });
      textBlock(ng, m.lines, p.x, p.y - (m.lines.length * size * 1.42) / 2, {
        size: size, weight: 600, fill: T.ink, anchor: 'middle'
      });
      reg(nd.id, ng, { kind: 'fade' });
    });

    // 中心文字
    if (spec.center_text) {
      var cSize = baseSize + 6;
      var cl = wrapText(spec.center_text, Math.min(rx * 1.5, 420), cSize, 800);
      var cg = el('g'); g.appendChild(cg);
      textBlock(cg, cl, cx, cy - (cl.length * cSize * 1.42) / 2, {
        size: cSize, weight: 800, fill: T.ink, anchor: 'middle'
      });
      reg(spec.center_id || 'center', cg, { kind: 'fade' });
    }
  }

  /* ============================ 图型：layers ============================ */

  function layoutLayers(spec, box) {
    var T = S.T, g = S.root;
    var layers = spec.layers || [];
    if (!layers.length) return;
    var baseSize = spec.font_size || 30;
    var totalRatio = layers.reduce(function (s, l) { return s + (l.ratio || 1); }, 0);
    var gapY = 12;
    var availH = box.h - gapY * (layers.length - 1);
    var y = box.y;

    layers.forEach(function (ly, i) {
      var h = Math.max(70, availH * (ly.ratio || 1) / totalRatio);
      var lg = el('g'); g.appendChild(lg);
      var color = ly.color || [T.a1, T.a3, T.a4, T.a2][i % 4];
      rect(lg, {
        x: box.x, y: y, w: box.w, h: h, r: 14,
        fill: ly.fill || T.card, stroke: color, sw: 2,
        dash: ly.dash || null
      });
      rect(lg, { x: box.x, y: y, w: 8, h: h, r: 4, fill: color });

      var labelSize = Math.min(baseSize + 4, h * 0.34);
      var labelLines = wrapText(ly.label || '', box.w * 0.42, labelSize, 800);
      var lh = labelLines.length * labelSize * 1.42;
      textBlock(lg, labelLines, box.x + 34, y + (h - lh) / 2, {
        size: labelSize, weight: 800, fill: T.ink, maxW: box.w * 0.42
      });
      if (ly.detail) {
        var dSize = Math.max(18, Math.min(baseSize - 6, h * 0.24));
        var dLines = wrapText(ly.detail, box.w * 0.5, dSize, 400);
        var dh = dLines.length * dSize * 1.42;
        textBlock(lg, dLines, box.x + box.w - 30, y + (h - dh) / 2, {
          size: dSize, weight: 400, fill: T.sub, anchor: 'end', maxW: box.w * 0.5
        });
      }
      reg(ly.id, lg, { kind: 'fade' });
      y += h + gapY;
    });
  }

  /* ============================ 图型：board（自由板书） ============================ */

  function roughD(pts, amp) {
    if (pts.length < 2) return '';
    var d = 'M' + pts[0][0] + ',' + pts[0][1];
    for (var i = 1; i < pts.length; i++) {
      var a = pts[i - 1], b = pts[i];
      var segLen = Math.hypot(b[0] - a[0], b[1] - a[1]);
      var steps = Math.max(1, Math.round(segLen / 60));
      for (var s = 1; s <= steps; s++) {
        var t = s / steps;
        var x = a[0] + (b[0] - a[0]) * t + (rnd() - 0.5) * amp * 2;
        var y = a[1] + (b[1] - a[1]) * t + (rnd() - 0.5) * amp * 2;
        d += ' L' + x.toFixed(1) + ',' + y.toFixed(1);
      }
    }
    return d;
  }

  function layoutBoard(spec, box) {
    var T = S.T, g = S.root;
    var baseSize = spec.font_size || 36;
    var rough = spec.rough !== false;
    var amp = spec.rough_amp || 2.2;
    seedReset(spec.seed || 20261022);

    function colorOf(c) {
      if (!c) return T.ink;
      if (c === 'accent' || c === 'a1') return T.a1;
      if (c === 'a2') return T.a2;
      if (c === 'a3') return T.a3;
      if (c === 'a4') return T.a4;
      return c;
    }

    (spec.strokes || []).forEach(function (st) {
      var sg = el('g'); g.appendChild(sg);
      var color = colorOf(st.color);
      if (st.points && st.points.length) {
        var pts = st.points.slice();
        if (st.closed) pts.push(pts[0]);
        path(sg, {
          d: rough ? roughD(pts, amp) : ('M' + pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' L')),
          stroke: color, sw: st.width || 5, dash: st.dash ? '14 10' : null,
          markerEnd: st.arrow ? 'url(#' + ensureMarker(color) + ')' : null
        });
      } else if (st.d) {
        path(sg, { d: st.d, stroke: color, sw: st.width || 5, dash: st.dash ? '14 10' : null });
      }
      reg(st.id, sg, { kind: 'stroke' });
    });

    (spec.circles || []).forEach(function (c) {
      var cg = el('g'); g.appendChild(cg);
      var color = colorOf(c.color);
      if (c.r) {
        circle(cg, { cx: c.x, cy: c.y, r: c.r, stroke: color, sw: c.width || 5 });
      } else if (c.rx != null) {
        var e = el('ellipse', {
          cx: c.x, cy: c.y, rx: c.rx, ry: c.ry || c.rx,
          fill: 'none', stroke: color, 'stroke-width': c.width || 5
        });
        cg.appendChild(e);
      }
      reg(c.id, cg, { kind: 'stroke' });
    });

    (spec.arrows || []).forEach(function (ar) {
      var ag = el('g'); g.appendChild(ag);
      var color = colorOf(ar.color);
      var d = rough
        ? roughD([ar.from, ar.to], amp * 0.5)
        : 'M' + ar.from[0] + ',' + ar.from[1] + ' L' + ar.to[0] + ',' + ar.to[1];
      path(ag, {
        d: d, stroke: color, sw: ar.width || 5,
        markerEnd: 'url(#' + ensureMarker(color) + ')'
      });
      reg(ar.id, ag, { kind: 'stroke' });
    });

    (spec.notes || []).forEach(function (nt) {
      if (nt.anchor === 'middle' && !nt.x2) {
        // 自动按宽居中
      }
      var ng = el('g'); g.appendChild(ng);
      var size = nt.size || baseSize;
      var lines = wrapText(nt.text || '', nt.maxW || (box.w - 120), size, nt.weight || 600);
      if (nt.bg) {
        var tw = 0;
        lines.forEach(function (l) { tw = Math.max(tw, textW(l, size, nt.weight || 600)); });
        var hh = lines.length * size * 1.42;
        rect(ng, {
          x: nt.x - 18, y: nt.y - 14, w: tw + 36, h: hh + 24, r: 10,
          fill: nt.bg, stroke: 'none', opacity: 0.9
        });
      }
      textBlock(ng, lines, nt.x, nt.y, {
        size: size, weight: nt.weight || 600, fill: colorOf(nt.color), maxW: nt.maxW || (box.w - 120)
      });
      reg(nt.id, ng, { kind: 'fade' });
    });
  }

  /* ============================ 布局分发 ============================ */

  var TYPES = ['flow', 'concept', 'timeline', 'compare', 'curve', 'cycle', 'layers', 'board'];

  var LAYOUTS = {
    flow: layoutFlow,
    concept: layoutConcept,
    timeline: layoutTimeline,
    compare: layoutCompare,
    curve: layoutCurve,
    cycle: layoutCycle,
    layers: layoutLayers,
    board: layoutBoard
  };

  /* ============================ 动画 ============================ */

  function strokeTargets(node) {
    if (node.tagName.toLowerCase() === 'g') {
      return Array.prototype.slice.call(node.querySelectorAll('path,line,polyline,polygon,circle,ellipse,rect'));
    }
    return [node];
  }

  function clearAnims(node) {
    try {
      node.getAnimations ? node.getAnimations({ subtree: true }).forEach(function (a) { a.cancel(); })
        : node.querySelectorAll('*').forEach(function (c) { if (c.getAnimations) c.getAnimations().forEach(function (a) { a.cancel(); }); });
    } catch (e) { /* noop */ }
  }

  function setHidden(it) {
    it.node.style.opacity = '0';
    it.node.style.visibility = 'visible';
    if (it.kind === 'stroke') {
      strokeTargets(it.node).forEach(function (t) {
        if (t._len == null) {
          try { t._len = t.getTotalLength(); } catch (e) { t._len = 0; }
        }
        if (t._len) {
          t.style.strokeDasharray = t._len;
          t.style.strokeDashoffset = t._len;
        }
      });
    }
  }

  function setShown(it) {
    it.node.style.opacity = '1';
    it.node.style.transform = '';
    if (it.kind === 'stroke') {
      strokeTargets(it.node).forEach(function (t) {
        if (t._len) {
          t.style.strokeDasharray = t._len;
          t.style.strokeDashoffset = 0;
        }
      });
    }
  }

  function animateIn(it, dur, delay) {
    it.node.style.opacity = '1';
    if (it.kind === 'stroke') {
      var i = 0;
      var targets = strokeTargets(it.node);
      var step = dur / Math.max(targets.length, 1);
      targets.forEach(function (t) {
        if (t._len == null) { try { t._len = t.getTotalLength(); } catch (e) { t._len = 0; } }
        if (!t._len) {
          t.style.strokeDasharray = '';
          t.style.strokeDashoffset = 0;
          if (t.animate) t.animate([{ opacity: 0 }, { opacity: 1 }], { duration: dur, delay: delay, fill: 'both', easing: 'ease-out' });
          return;
        }
        t.style.strokeDasharray = t._len;
        t.style.strokeDashoffset = t._len;
        if (t.animate) {
          t.animate(
            [{ strokeDashoffset: t._len }, { strokeDashoffset: 0 }],
            { duration: Math.max(180, dur), delay: delay + i * Math.min(step, 220), fill: 'forwards', easing: 'ease-in-out' }
          );
        } else {
          t.style.strokeDashoffset = 0;
        }
        i++;
      });
    } else {
      if (it.node.animate) {
        it.node.animate(
          [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }],
          { duration: dur, delay: delay, fill: 'both', easing: 'cubic-bezier(.22,.68,.35,1)' }
        );
      }
    }
  }

  /* ============================ 主入口 ============================ */

  function mount(spec, opts) {
    opts = opts || {};
    var themeName = spec.theme || 'light';
    var T = THEMES[themeName] || THEMES.light;
    resetState(spec, T);

    var cv = CANVAS[spec.canvas || (opts.canvas) || 'portrait'] || CANVAS.portrait;
    S.W = cv.w; S.H = cv.h;
    var pad = spec.padding == null ? Math.round(Math.min(cv.w, cv.h) * 0.062) : spec.padding;
    S.pad = pad;

    var stage = opts.stage;
    stage.innerHTML = '';

    var svg = el('svg', {
      viewBox: '0 0 ' + cv.w + ' ' + cv.h,
      preserveAspectRatio: 'xMidYMid meet',
      xmlns: NS
    });
    svg.style.cssText = 'width:100%;height:100%;display:block;background:' + T.bg;
    svg.setAttribute('font-family', FONT);

    var defs = el('defs');
    svg.appendChild(defs);
    S.defs = defs;

    // 背景（网格可选）
    var bgG = el('g');
    svg.appendChild(bgG);
    rect(bgG, { x: 0, y: 0, w: cv.w, h: cv.h, r: 0, fill: T.bg });
    if (spec.grid) {
      var step = 60;
      for (var gx = step; gx < cv.w; gx += step) {
        path(bgG, { d: 'M' + gx + ',0 L' + gx + ',' + cv.h, stroke: T.line, sw: 0.8, opacity: 0.30 });
      }
      for (var gy = step; gy < cv.h; gy += step) {
        path(bgG, { d: 'M0,' + gy + ' L' + cv.w + ',' + gy, stroke: T.line, sw: 0.8, opacity: 0.30 });
      }
    }

    var root = el('g');
    svg.appendChild(root);

    // 标题区（不参与内容缩放，始终按画布基准字号）
    var topOffset = pad;
    if (spec.title) {
      var titleSize = spec.title_size || Math.round(Math.min(cv.w, cv.h) * 0.055);
      var tg = el('g'); root.appendChild(tg);
      var tl = wrapText(spec.title, cv.w - pad * 2 - 20, titleSize, 800);
      textBlock(tg, tl, pad, pad, { size: titleSize, weight: 800, fill: T.ink, maxW: cv.w - pad * 2 });
      var usedH = tl.length * titleSize * 1.42;
      if (spec.subtitle) {
        var ss = Math.round(titleSize * 0.5);
        textBlock(tg, spec.subtitle, pad, pad + usedH + 8, { size: ss, weight: 500, fill: T.sub, maxW: cv.w - pad * 2 });
        usedH += ss * 1.42 + 10;
      }
      // 强调下划线
      path(tg, {
        d: 'M' + pad + ',' + (pad + usedH + 20) + ' L' + (pad + Math.min(160, cv.w * 0.16)) + ',' + (pad + usedH + 20),
        stroke: T.a1, sw: 6
      });
      reg('_title', tg, { kind: 'fade' });
      topOffset = pad + usedH + 52;
    }

    var box = {
      x: pad, y: topOffset,
      w: cv.w - pad * 2,
      h: cv.h - topOffset - pad
    };

    // 内容层独立成组，便于整体自适应缩放
    var content = el('g');
    root.appendChild(content);
    S.root = content;

    var layoutFn = LAYOUTS[spec.type];
    if (!layoutFn) {
      throw new Error('未知的 type: ' + spec.type + '，可用：' + TYPES.join(' / '));
    }
    layoutFn(spec, box);

    stage.appendChild(svg);

    // ---- 内容自适应：溢出则缩，偏小则放（上限 1.5，标题不缩放）----
    var scaleFix = 1;
    try {
      if (!S.noAutoScale) {
        var bb = content.getBBox();
        var availW = cv.w - pad * 2;
        var availH = cv.h - box.y - pad;
        if (bb.width > 1 && bb.height > 1 && availW > 0 && availH > 0) {
          var k = Math.min(availW / bb.width, availH / bb.height);
          k = k < 0.995 ? k : Math.min(k, 1.5);
          if (Math.abs(k - 1) > 0.012) {
            scaleFix = k;
            var tx = pad + (availW - bb.width * k) / 2 - bb.x * k;
            var ty = box.y + (availH - bb.height * k) / 2 - bb.y * k;
            content.setAttribute('transform', 'translate(' + tx + ',' + ty + ') scale(' + k + ')');
          }
        }
      }
    } catch (e) { /* noop */ }

    // ---- 步骤规划 ----
    var steps = spec.steps;
    var order = S.items.map(function (it) { return it.id; });

    if (!steps || !steps.length) {
      // 自动：每个「主图元」一步；连线/子标签有 owner，跟随主图元出现，不单独占步
      var seen = {};
      steps = S.items.filter(function (it) {
        if (it.owner) return false;
        if (it.id.charAt(0) === '_') return false;
        if (seen[it.id]) return false;
        seen[it.id] = 1; return true;
      }).map(function (it) { return { reveal: [it.id] }; });
      if (!steps.length) steps = [{ reveal: [] }];
    }

    // id → step 索引
    var stepOf = {};
    steps.forEach(function (st, i) {
      var rv = st.reveal;
      if (typeof rv === 'string') rv = rv === 'auto' ? [order[i]] : [rv];
      if (!rv) rv = [];
      rv.forEach(function (id) { stepOf[id] = i; });
    });

    S.items.forEach(function (it) {
      if (stepOf[it.id] != null) { it.step = stepOf[it.id]; return; }
      if (it.owner && stepOf[it.owner] != null) { it.step = stepOf[it.owner]; return; }
      it.step = 0; // 未规划的：与第一步同现，避免永久隐藏
    });

    // 初始全隐
    S.items.forEach(setHidden);

    // ---- 播放控制 ----
    var cur = 0;
    var playing = false;
    var timer = null;

    function stepDur(st) {
      if (st.seconds) return st.seconds * 1000;
      var text = st.narration || '';
      return clamp(text.length / 4.5 * 1000 + 1200, 2200, 16000);
    }

    function renderStep(curN, animate) {
      var total = steps.length;
      var idx = curN - 1;   // curN 是 1 基的步序号，idx 是 steps 的 0 基下标
      S.items.forEach(function (it) {
        // 标题始终常显
        if (it.id === '_title') { setShown(it); return; }
        if (idx >= 0 && it.step < idx) {
          setShown(it);
        } else if (it.step === idx && animate) {
          clearAnims(it.node);
          setHidden(it);
          animateIn(it, it.kind === 'stroke' ? 620 : 460, 0);
        } else if (it.step === idx) {
          clearAnims(it.node); setShown(it);
        } else {
          clearAnims(it.node); setHidden(it);
        }
      });
      if (opts.onStep) opts.onStep(curN, total, steps[curN - 1] || null);
    }

    function go(target, animate) {
      target = clamp(target, 0, steps.length);
      var dir = target >= cur ? 1 : -1;
      cur = target;
      renderStep(cur, animate !== false);
      updateUI();
    }

    function next() {
      if (cur >= steps.length) return false;
      go(cur + 1, true);
      return cur < steps.length;
    }

    function prev() {
      if (cur <= 0) return false;
      // 回退直接跳到目标步的静态态
      go(cur - 1, false);
      return true;
    }

    function updateUI() {
      if (opts.onUpdate) opts.onUpdate(cur, steps.length, steps[cur - 1] || null, playing);
    }

    function play() {
      if (playing) { stop(); return; }
      if (cur >= steps.length) go(0, false);
      playing = true;
      updateUI();
      (function tick() {
        if (!playing) return;
        var ok = next();
        if (!ok) { stop(); return; }
        var st = steps[cur - 1];
        timer = setTimeout(tick, stepDur(st));
      })();
    }

    function stop() {
      playing = false;
      if (timer) clearTimeout(timer);
      timer = null;
      updateUI();
    }

    function replay() { stop(); go(0, false); setTimeout(function () { play(); }, 120); }

    return {
      go: go, next: next, prev: prev, play: play, stop: stop, replay: replay,
      get index() { return cur; },
      get total() { return steps.length; },
      steps: steps,
      svg: svg,
      theme: themeName,
      scale: scaleFix
    };
  }

  global.TeachDiagram = {
    version: '1.0.0',
    mount: mount,
    themes: Object.keys(THEMES),
    canvases: Object.keys(CANVAS),
    types: TYPES
  };

})(typeof window !== 'undefined' ? window : this);
