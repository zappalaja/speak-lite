/* SPEAR-MED windy-style viewer frontend.
 *
 * Fetches (var, experiment, member|ensmean, month) fields from /api/field
 * and renders them as colormapped rasters reprojected to Web Mercator.
 * Compare mode fetches two selections (A, B) and renders A − B with a
 * diverging colormap auto-scaled to the difference field. A canvas
 * particle layer advects particles through the uas/vas wind field.
 */
"use strict";

// ---------------------------------------------------------------- colormaps
// Windy-style multi-hue ramps. stops: value (display units), rgb, alpha.
const VAR_CONFIG = {
  pr: {
    decimals: 2,
    legend: { min: 0, max: 30, ticks: [0, 5, 10, 15, 20, 25, 30] },
    stops: [
      { v: 0.0,  c: [26, 47, 107],  a: 0.0  },
      { v: 0.4,  c: [26, 47, 107],  a: 0.3  },
      { v: 1.0,  c: [32, 80, 176],  a: 0.62 },
      { v: 2.0,  c: [47, 124, 208], a: 0.78 },
      { v: 4.0,  c: [64, 181, 196], a: 0.86 },
      { v: 7.0,  c: [62, 183, 118], a: 0.89 },
      { v: 10.0, c: [97, 201, 73],  a: 0.9  },
      { v: 13.0, c: [170, 212, 66], a: 0.91 },
      { v: 16.0, c: [222, 214, 73], a: 0.92 },
      { v: 22.0, c: [227, 154, 59], a: 0.93 },
      { v: 30.0, c: [217, 95, 48],  a: 0.94 },
      { v: 42.0, c: [201, 60, 92],  a: 0.95 },
    ],
  },
  tas: {
    decimals: 1,
    legend: { min: -40, max: 40, ticks: [-40, -20, 0, 20, 40] },
    stops: [
      { v: -40, c: [135, 68, 153], a: 0.86 },
      { v: -30, c: [95, 70, 190],  a: 0.86 },
      { v: -20, c: [57, 97, 207],  a: 0.86 },
      { v: -10, c: [69, 144, 215], a: 0.86 },
      { v: 0,   c: [85, 195, 205], a: 0.86 },
      { v: 8,   c: [116, 208, 140],a: 0.86 },
      { v: 16,  c: [175, 213, 82], a: 0.86 },
      { v: 22,  c: [228, 205, 66], a: 0.86 },
      { v: 28,  c: [231, 153, 53], a: 0.86 },
      { v: 34,  c: [227, 101, 46], a: 0.86 },
      { v: 40,  c: [200, 50, 68],  a: 0.86 },
    ],
  },
  psl: {
    decimals: 0,
    legend: { min: 960, max: 1050, ticks: [960, 990, 1020, 1050] },
    stops: [
      { v: 960,  c: [120, 45, 160], a: 0.85 },
      { v: 980,  c: [75, 65, 200],  a: 0.85 },
      { v: 992,  c: [48, 105, 220], a: 0.85 },
      { v: 1002, c: [58, 168, 200], a: 0.85 },
      { v: 1012, c: [100, 200, 120],a: 0.85 },
      { v: 1022, c: [195, 210, 70], a: 0.85 },
      { v: 1032, c: [232, 158, 50], a: 0.85 },
      { v: 1048, c: [222, 90, 48],  a: 0.85 },
    ],
  },
  sfcWind: {
    decimals: 1,
    legend: { min: 0, max: 30, ticks: [0, 10, 20, 30] },
    stops: [
      { v: 0,  c: [55, 65, 110],  a: 0.25 },
      { v: 3,  c: [48, 90, 170],  a: 0.6  },
      { v: 6,  c: [52, 140, 200], a: 0.8  },
      { v: 9,  c: [62, 185, 175], a: 0.85 },
      { v: 12, c: [95, 200, 105], a: 0.88 },
      { v: 16, c: [185, 210, 70], a: 0.9  },
      { v: 20, c: [230, 155, 55], a: 0.92 },
      { v: 25, c: [220, 90, 55],  a: 0.94 },
      { v: 30, c: [175, 55, 115], a: 0.95 },
    ],
  },
  // TOA radiation: low OLR = cold high cloud tops -> bright white,
  // satellite-IR style; clear sky fades to the basemap.
  rlut: {
    decimals: 0,
    legend: { min: 100, max: 320, ticks: [100, 160, 220, 280] },
    stops: [
      { v: 100, c: [255, 255, 255], a: 0.95 },
      { v: 150, c: [225, 230, 240], a: 0.85 },
      { v: 190, c: [170, 185, 205], a: 0.6  },
      { v: 230, c: [105, 120, 145], a: 0.35 },
      { v: 270, c: [55, 65, 85],    a: 0.15 },
      { v: 320, c: [10, 10, 15],    a: 0.02 },
    ],
  },
  rsut: {
    decimals: 0,
    legend: { min: 0, max: 300, ticks: [0, 100, 200, 300] },
    stops: [
      { v: 0,   c: [0, 0, 0],       a: 0    },
      { v: 60,  c: [85, 100, 125],  a: 0.3  },
      { v: 120, c: [135, 150, 175], a: 0.55 },
      { v: 180, c: [185, 196, 215], a: 0.75 },
      { v: 240, c: [225, 232, 245], a: 0.88 },
      { v: 300, c: [255, 255, 255], a: 0.95 },
    ],
  },
  rsdt: {
    decimals: 0,
    legend: { min: 0, max: 550, ticks: [0, 150, 300, 450] },
    stops: [
      { v: 0,   c: [35, 25, 80],   a: 0.7  },
      { v: 140, c: [59, 82, 139],  a: 0.78 },
      { v: 280, c: [33, 145, 140], a: 0.82 },
      { v: 420, c: [147, 213, 72], a: 0.86 },
      { v: 550, c: [253, 231, 37], a: 0.9  },
    ],
  },
  tos: {
    decimals: 1,
    legend: { min: -2, max: 32, ticks: [0, 10, 20, 30] },
    stops: [
      { v: -2, c: [135, 70, 165], a: 0.88 },
      { v: 2,  c: [72, 82, 200],  a: 0.88 },
      { v: 8,  c: [57, 120, 215], a: 0.88 },
      { v: 14, c: [80, 190, 205], a: 0.88 },
      { v: 20, c: [120, 210, 130],a: 0.88 },
      { v: 25, c: [225, 205, 70], a: 0.88 },
      { v: 29, c: [230, 140, 55], a: 0.88 },
      { v: 32, c: [205, 55, 60],  a: 0.88 },
    ],
  },
  // Pressure-level fields: value ranges shift drastically with level
  // (500 hPa heights ~5500 m vs 850 hPa ~1400 m), so these auto-scale
  // to the loaded field (2nd-98th percentile) instead of fixed stops.
  ta:  { decimals: 1, dynamic: "seq", ramp: "thermal" },
  ua:  { decimals: 1, dynamic: "div" },
  va:  { decimals: 1, dynamic: "div" },
  // surface wind components: signed, so diverging + auto-scaled like ua/va
  uas: { decimals: 1, dynamic: "div" },
  vas: { decimals: 1, dynamic: "div" },
  zg:  { decimals: 0, dynamic: "seq", ramp: "plasma" },
  hus: { decimals: 2, dynamic: "seq", ramp: "blues" },
};

const RAMPS = {
  thermal: [[135, 68, 153], [57, 97, 207], [85, 195, 205], [116, 208, 140],
            [228, 205, 66], [231, 153, 53], [200, 50, 68]],
  plasma: [[13, 8, 135], [126, 3, 168], [203, 71, 119], [248, 149, 64], [240, 249, 33]],
  blues: [[25, 35, 80], [24, 79, 149], [57, 135, 229], [122, 184, 240], [205, 226, 251]],
};

// Display-unit options per variable. Data stays in base units everywhere
// (colormaps are keyed to physical values, so the raster never re-renders);
// conversion happens only in the legend, status line, and click readout.
const TEMP_UNITS = [
  { id: "c", label: "°C", scale: 1, offset: 0, unit: "°C", decimals: 1 },
  { id: "f", label: "°F", scale: 9 / 5, offset: 32, unit: "°F", decimals: 1 },
  { id: "k", label: "K", scale: 1, offset: 273.15, unit: "K", decimals: 1 },
];
const WIND_UNITS = [
  { id: "ms", label: "m/s", scale: 1, offset: 0, unit: "m/s", decimals: 1 },
  { id: "mph", label: "mph", scale: 2.23694, offset: 0, unit: "mph", decimals: 1 },
  { id: "kt", label: "kt", scale: 1.94384, offset: 0, unit: "kt", decimals: 1 },
  { id: "kmh", label: "km/h", scale: 3.6, offset: 0, unit: "km/h", decimals: 1 },
];
const RAD_UNITS = [
  { id: "wm2", label: "W/m²", scale: 1, offset: 0, unit: "W/m²", decimals: 0 },
];

const UNITS = {
  pr: [
    { id: "mmday", label: "mm/day", scale: 1, offset: 0, unit: "mm/day", decimals: 2 },
    { id: "inday", label: "in/day", scale: 1 / 25.4, offset: 0, unit: "in/day", decimals: 3 },
    // native store units (values ~1e-5, so scientific notation)
    { id: "si", label: "kg m⁻² s⁻¹", scale: 1 / 86400, offset: 0, unit: "kg·m⁻²·s⁻¹", decimals: 2, sci: true },
  ],
  tas: TEMP_UNITS,
  ta: TEMP_UNITS,
  tos: TEMP_UNITS,
  psl: [
    { id: "hpa", label: "hPa", scale: 1, offset: 0, unit: "hPa", decimals: 0 },
    { id: "inhg", label: "inHg", scale: 0.02953, offset: 0, unit: "inHg", decimals: 2 },
  ],
  sfcWind: WIND_UNITS,
  uas: WIND_UNITS,
  vas: WIND_UNITS,
  ua: WIND_UNITS,
  va: WIND_UNITS,
  rlut: RAD_UNITS,
  rsut: RAD_UNITS,
  rsdt: RAD_UNITS,
  zg: [
    { id: "m", label: "m", scale: 1, offset: 0, unit: "m", decimals: 0 },
    { id: "dam", label: "dam", scale: 0.1, offset: 0, unit: "dam", decimals: 1 },
    { id: "ft", label: "ft", scale: 3.28084, offset: 0, unit: "ft", decimals: 0 },
  ],
  hus: [
    { id: "kgkg", label: "kg/kg", scale: 1, offset: 0, unit: "kg/kg", decimals: 2, sci: true },
    { id: "gkg", label: "g/kg", scale: 1000, offset: 0, unit: "g/kg", decimals: 2 },
  ],
};

function unitSpecFor(varName) {
  const opts = UNITS[varName];
  return opts.find((u) => u.id === state.units[varName]) || opts[0];
}

function convVal(v, spec) {
  return v * spec.scale + spec.offset;
}

// Differences convert with the scale only — offsets cancel in A − B
// (e.g. a Δ of 5 °C is 9 °F, not 41 °F).
function convDiff(v, spec) {
  return v * spec.scale;
}

// Spread and anomaly are difference-like quantities: like compare-mode
// diffs, their unit conversion is scale-only.
function isDeltaLike(d) {
  return !!d && (d.isDiff || d.stat === "spread" || d.stat === "anom");
}

function convForDisplay(d, v, spec) {
  return isDeltaLike(d) ? convDiff(v, spec) : convVal(v, spec);
}

const SUP_DIGITS = { "-": "⁻", "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
                     "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹" };

// 2.36e-5 -> 2.36×10⁻⁵
function sciNotation(v, digits) {
  if (v === 0) return "0";
  const [mant, exp] = v.toExponential(digits).split("e");
  const supExp = String(parseInt(exp, 10)).replace(/[-0-9]/g, (c) => SUP_DIGITS[c]);
  return `${mant}×10${supExp}`;
}

function fmtTick(v) {
  if (v !== 0 && Math.abs(v) < 0.01) return sciNotation(v, 1);
  return String(Math.abs(v) >= 100 ? Math.round(v) : +v.toPrecision(3));
}

function fmtVal(v, spec) {
  return spec.sci ? sciNotation(v, 2) : v.toFixed(spec.decimals);
}

function colormap(stops, v, out) {
  if (v == null || Number.isNaN(v)) { out[3] = 0; return out; }
  if (v <= stops[0].v) {
    const s = stops[0];
    out[0] = s.c[0]; out[1] = s.c[1]; out[2] = s.c[2]; out[3] = Math.round(s.a * 255);
    return out;
  }
  const last = stops[stops.length - 1];
  if (v >= last.v) {
    out[0] = last.c[0]; out[1] = last.c[1]; out[2] = last.c[2];
    out[3] = Math.round(last.a * 255);
    return out;
  }
  for (let i = 1; i < stops.length; i++) {
    if (v <= stops[i].v) {
      const s0 = stops[i - 1], s1 = stops[i];
      const t = (v - s0.v) / (s1.v - s0.v);
      out[0] = Math.round(s0.c[0] + t * (s1.c[0] - s0.c[0]));
      out[1] = Math.round(s0.c[1] + t * (s1.c[1] - s0.c[1]));
      out[2] = Math.round(s0.c[2] + t * (s1.c[2] - s0.c[2]));
      out[3] = Math.round((s0.a + t * (s1.a - s0.a)) * 255);
      return out;
    }
  }
  return out;
}

// Precomputed color lookup tables make the 2M-pixel raster render fast
// enough to not freeze the UI (a per-pixel stop search is ~20x slower).
const lutCache = {};
function buildLUT(varName) {
  if (lutCache[varName]) return lutCache[varName];
  const stops = VAR_CONFIG[varName].stops;
  const N = 1024;
  const min = stops[0].v;
  const max = stops[stops.length - 1].v;
  const data = new Uint8ClampedArray(N * 4);
  const rgba = [0, 0, 0, 0];
  for (let i = 0; i < N; i++) {
    colormap(stops, min + ((max - min) * i) / (N - 1), rgba);
    data.set(rgba, i * 4);
  }
  return (lutCache[varName] = { min, max, scale: (N - 1) / (max - min), last: N - 1, data });
}

// Diverging colormap for compare mode: blue where A < B, red where A > B,
// fading to transparent at zero difference.
const DIV_NEG = [57, 135, 229];
const DIV_POS = [227, 73, 72];
const DIV_MID = [125, 125, 120];

function diffColor(v, limit, out) {
  if (v == null || Number.isNaN(v)) { out[3] = 0; return out; }
  let t = v / limit;
  if (t > 1) t = 1; else if (t < -1) t = -1;
  const a = Math.abs(t);
  const pole = t < 0 ? DIV_NEG : DIV_POS;
  const s = Math.sqrt(a);
  out[0] = Math.round(DIV_MID[0] + (pole[0] - DIV_MID[0]) * s);
  out[1] = Math.round(DIV_MID[1] + (pole[1] - DIV_MID[1]) * s);
  out[2] = Math.round(DIV_MID[2] + (pole[2] - DIV_MID[2]) * s);
  const alpha = a < 0.04 ? (a / 0.04) * 0.15 : 0.15 + ((a - 0.04) / 0.96) * 0.75;
  out[3] = Math.round(alpha * 255);
  return out;
}

function makeLUT(min, max, colorFn) {
  const N = 1024;
  const data = new Uint8ClampedArray(N * 4);
  const rgba = [0, 0, 0, 0];
  for (let i = 0; i < N; i++) {
    colorFn(min + ((max - min) * i) / (N - 1), rgba);
    data.set(rgba, i * 4);
  }
  return { min, max, scale: (N - 1) / (max - min), last: N - 1, data };
}

function buildDiffLUT(limit) {
  return makeLUT(-limit, limit, (v, out) => diffColor(v, limit, out));
}

// ---- dynamic (auto-scaled) colormaps for pressure-level fields
function samplePercentiles(arr, plo, phi) {
  const vals = [];
  for (let i = 0; i < arr.length; i += 4) {
    const v = arr[i];
    if (!Number.isNaN(v)) vals.push(v);
  }
  if (!vals.length) return [0, 1];
  vals.sort((a, b) => a - b);
  return [
    vals[Math.floor((vals.length - 1) * plo)],
    vals[Math.floor((vals.length - 1) * phi)],
  ];
}

function rampColor(rampName, t, alpha, out) {
  const ramp = RAMPS[rampName];
  const x = Math.min(1, Math.max(0, t)) * (ramp.length - 1);
  const i = Math.min(ramp.length - 2, x | 0);
  const f = x - i;
  out[0] = Math.round(ramp[i][0] + f * (ramp[i + 1][0] - ramp[i][0]));
  out[1] = Math.round(ramp[i][1] + f * (ramp[i + 1][1] - ramp[i][1]));
  out[2] = Math.round(ramp[i][2] + f * (ramp[i + 1][2] - ramp[i][2]));
  out[3] = Math.round(alpha * 255);
  return out;
}

// Computes the field's display range, stores it on f._dyn, returns a LUT.
function buildDynamicLUT(f, cfg) {
  if (cfg.dynamic === "div") {
    const [lo, hi] = samplePercentiles(f._grid, 0.02, 0.98);
    const L = niceLimit(Math.max(Math.abs(lo), Math.abs(hi)));
    f._dyn = { min: -L, max: L, limit: L, div: true };
    return makeLUT(-L, L, (v, out) => diffColor(v, L, out));
  }
  let [lo, hi] = samplePercentiles(f._grid, 0.02, 0.98);
  if (!(hi > lo)) hi = lo + 1;
  f._dyn = { min: lo, max: hi, ramp: cfg.ramp };
  return makeLUT(lo, hi, (v, out) =>
    rampColor(cfg.ramp, (v - lo) / (hi - lo), 0.88, out)
  );
}

// LUT choice per statistic: spread is always dynamic-sequential, anomaly
// always dynamic-diverging (centered on zero); otherwise the variable's own.
function lutForField(f) {
  const cfg = VAR_CONFIG[f.var];
  if (f.stat === "anom") return buildDynamicLUT(f, { dynamic: "div" });
  if (f.stat === "spread") return buildDynamicLUT(f, { dynamic: "seq", ramp: "plasma" });
  return cfg.dynamic ? buildDynamicLUT(f, cfg) : buildLUT(f.var);
}

function statPrefix(f) {
  return f.stat === "spread" ? "Spread of " : f.stat === "anom" ? "Anomaly of " : "";
}

// Symmetric display limit for a difference field: 98th percentile of |Δ|,
// rounded up to a "nice" number (1/2/2.5/5 × 10^k).
function niceLimit(x) {
  if (!(x > 0)) return 1;
  const k = Math.pow(10, Math.floor(Math.log10(x)));
  for (const m of [1, 2, 2.5, 5, 10]) if (x <= m * k * 1.0001) return m * k;
  return 10 * k;
}

function diffLimit(arr) {
  const vals = [];
  for (let i = 0; i < arr.length; i += 4) {
    const v = arr[i];
    if (!Number.isNaN(v)) vals.push(Math.abs(v));
  }
  if (!vals.length) return 1;
  vals.sort((x, y) => x - y);
  return niceLimit(vals[Math.floor((vals.length - 1) * 0.98)]);
}

// ---------------------------------------------------------------- map
// Snap every Leaflet-positioned element (tooltips/station cards, labels,
// markers, panes) to whole pixels — fractional translate3d offsets place
// text on subpixel boundaries and visibly blur it.
{
  const _setPos = L.DomUtil.setPosition;
  L.DomUtil.setPosition = function (el, point) {
    _setPos(el, point.round());
  };
}

const MERC_LAT = 85.0511287798;

// Offsets (in degrees longitude) at which every non-tile layer is duplicated
// so the map stays continuous as the user pans across the antimeridian.
// Five copies (±720°) so even a very wide window at min zoom, panned to a
// neighbouring world copy, never reaches an uncovered edge.
const WORLD_COPIES = [-720, -360, 0, 360, 720];

const map = L.map("map", {
  center: [22, 10],
  zoom: 3,
  minZoom: 2,
  maxZoom: 9,
  worldCopyJump: true,
  maxBounds: [[-MERC_LAT, -1000], [MERC_LAT, 1000]],
  maxBoundsViscosity: 1.0,
  closePopupOnClick: false, // Ctrl+click station placement must not close the readout
  zoomControl: true,
});

// Never allow zooming out past the point where the world map is shorter
// than the viewport — that would expose empty void above/below the poles.
function enforceMinZoom() {
  const h = map.getSize().y;
  const mz = Math.max(2, Math.ceil(Math.log2(h / 256)));
  map.setMinZoom(mz);
  if (map.getZoom() < mz) map.setZoom(mz);
}
map.whenReady(enforceMinZoom);
map.on("resize", enforceMinZoom);

// Stacking: basemap (200) < data overlay (400) < borders < particles < labels < popups (700)
map.createPane("borders").style.zIndex = 430;
map.createPane("particles").style.zIndex = 440;
map.createPane("labels").style.zIndex = 450;
map.getPane("borders").style.pointerEvents = "none";
map.getPane("particles").style.pointerEvents = "none";
map.getPane("labels").style.pointerEvents = "none";

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
    '&copy; <a href="https://carto.com/attributions">CARTO</a> · SPEAR-MED (NOAA GFDL)',
  subdomains: "abcd",
}).addTo(map);

// Place labels above the data so geography stays readable through the
// field. Esri's Dark Gray Reference layer uses English place names
// everywhere (CARTO's label tiles use local-language names).
L.tileLayer(
  "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
  {
    pane: "labels",
    maxNativeZoom: 12,
    attribution: "Labels &copy; Esri",
  }
).addTo(map);

// Our own labels fill only what Esri lacks: continent/ocean names at low
// zooms and US state names.
map.createPane("geoLabels").style.zIndex = 448;
map.getPane("geoLabels").style.pointerEvents = "none";

const GEO_LABELS = [
  { name: "North America", lat: 46, lon: -101, max: 3 },
  { name: "South America", lat: -14, lon: -60, max: 3 },
  { name: "Europe", lat: 51, lon: 14, max: 3 },
  { name: "Africa", lat: 4, lon: 21, max: 3 },
  { name: "Asia", lat: 52, lon: 95, max: 3 },
  { name: "Australia", lat: -25, lon: 134, max: 3 },
  { name: "Antarctica", lat: -74, lon: 15, max: 3 },
  { name: "Pacific Ocean", lat: -5, lon: -145, max: 5, ocean: true },
  { name: "Atlantic Ocean", lat: 26, lon: -41, max: 5, ocean: true },
  { name: "Indian Ocean", lat: -22, lon: 79, max: 5, ocean: true },
  { name: "Southern Ocean", lat: -62, lon: 80, max: 5, ocean: true },
];

const geoLabelMarkers = []; // {el, min, max}

function addGeoLabel(name, lat, lon, cls, min, max, copies = WORLD_COPIES) {
  for (const offset of copies) {
    const icon = L.divIcon({
      className: "geo-label-anchor",
      html: `<span class="geo-label${cls ? " " + cls : ""}">${name}</span>`,
      iconSize: null,
    });
    const m = L.marker([lat, lon + offset], {
      icon,
      pane: "geoLabels",
      interactive: false,
      keyboard: false,
    }).addTo(map);
    geoLabelMarkers.push({ el: m.getElement(), min, max });
  }
}

function addGeoLabels() {
  for (const g of GEO_LABELS) {
    addGeoLabel(g.name, g.lat, g.lon, g.ocean ? "ocean" : "", 0, g.max);
  }
  updateGeoLabels();
}

// Country names, z4-7 — absent from the Esri reference tiles in our zoom
// range. Major countries (label rank <= 3) appear at z4 as the continents
// hand off; the rest join at z5.
async function addCountryLabels() {
  const countries = await (await fetch("geo/country_labels.json")).json();
  for (const c of countries) {
    addGeoLabel(c.n, c.lat, c.lon, "country", c.r <= 3 ? 4 : 5, 7);
  }
  updateGeoLabels();
}

// US state names — the Esri reference layer only labels cities here.
// Small northeastern states wait until zoom 6 to avoid label pile-ups.
const SMALL_STATES = new Set([
  "Connecticut", "Rhode Island", "Massachusetts", "New Jersey", "Delaware",
  "Maryland", "District of Columbia", "Vermont", "New Hampshire",
]);

async function addStateLabels() {
  const states = await (await fetch("geo/us_state_labels.json")).json();
  for (const s of states) {
    addGeoLabel(s.n, s.lat, s.lon, "state", SMALL_STATES.has(s.n) ? 6 : 5, 9);
  }
  updateGeoLabels();
}

function updateGeoLabels() {
  const z = map.getZoom();
  for (const { el, min, max } of geoLabelMarkers) {
    if (el) el.style.display = z >= min && z <= max ? "" : "none";
  }
}

map.on("zoomend", updateGeoLabels);

// Coastlines + country borders + US states, drawn just under the labels.
const BORDER_LAYERS = [
  { url: "geo/ne_50m_coastline.json", style: { color: "#e8e6df", weight: 1.1, opacity: 0.55 } },
  { url: "geo/ne_50m_admin_0_boundary_lines_land.json", style: { color: "#d9d7cf", weight: 1.0, opacity: 0.6 } },
  { url: "geo/ne_50m_admin_1_states_provinces_lines.json", style: { color: "#c9c7bf", weight: 0.9, opacity: 0.55 } },
];

async function loadBorders() {
  // One shared canvas renderer: ~6k SVG <path> nodes made every map
  // interaction repaint the whole vector DOM (multi-second frames).
  const renderer = L.canvas({ pane: "borders" });
  for (const spec of BORDER_LAYERS) {
    const geo = await (await fetch(spec.url)).json();
    for (const offset of WORLD_COPIES) {
      L.geoJSON(geo, {
        renderer,
        pane: "borders",
        interactive: false,
        style: spec.style,
        coordsToLatLng: (c) => L.latLng(c[1], c[0] + offset),
      }).addTo(map);
    }
  }
}

// ---------------------------------------------------------------- state
let currentLUT = null;      // LUT used for the current overlay (for re-renders)
let currentField = null;    // selection A /api/field payload (with _grid)
let currentFieldB = null;   // selection B payload in compare mode
let currentWind = null;     // /api/wind payload (with _u/_v), null in compare
let currentDisplay = null;  // what the overlay shows: a field or a diff object
let overlays = [];          // one L.imageOverlay per world copy
let overlayUrl = null;      // object URL of the current overlay image
let meta = null;
let loadSeq = 0;            // guards against out-of-order responses

// Selection state lives here, not in native <select> elements — their OS
// popups don't register clicks reliably in some WSL/remote environments.
const state = {
  var: "pr",
  experiment: "scenarioSSP5-85",
  member: "r1i1p1f1",
  stat: "raw",   // raw | mean | spread | anom
  compare: false,
  plev: 500,
  b: { experiment: "scenarioSSP5-85", member: "r2i1p1f1", stat: "raw" },
  units: {
    pr: "mmday", tas: "c", psl: "hpa", sfcWind: "ms", uas: "ms", vas: "ms",
    ta: "c", tos: "c", ua: "ms", va: "ms", zg: "m", hus: "kgkg",
    rlut: "wm2", rsut: "wm2", rsdt: "wm2",
  },
  // Display filter in BASE units (null = unbounded); values outside
  // [lo, hi] render transparent. Session-only, reset on variable change.
  filter: { lo: null, hi: null },
};

const el = (id) => document.getElementById(id);
const statusEl = el("status");

// ---- selection persistence across page refreshes
const SAVED = (() => {
  try {
    return JSON.parse(localStorage.getItem("spearViewer") || "null");
  } catch {
    return null;
  }
})();

// lat/lon graticule toggles — shared by the flat map and the globe
const gratState = {
  lat: !(SAVED && SAVED.grat && SAVED.grat.lat === false),
  lon: !(SAVED && SAVED.grat && SAVED.grat.lon === false),
};

function saveState() {
  try {
    localStorage.setItem("spearViewer", JSON.stringify({
      var: state.var,
      experiment: state.experiment,
      member: state.member,
      stat: state.stat,
      compare: state.compare,
      plev: state.plev,
      b: state.b,
      units: state.units,
      timeA: el("month-input").value,
      timeB: el("month-input-b").value,
      opacity: el("opacity").value,
      particles: el("particles-toggle").checked,
      pins: (typeof pinnedPoints !== "undefined" ? pinnedPoints : []).map((p) => ({
        lat: p.latlng.lat,
        lng: p.latlng.lng,
        collapsed: !!p.collapsed,
        job: p.tsReady ? p.tsJob : null,
      })),
      grat: { lat: gratState.lat, lon: gratState.lon },
      globe: (typeof globe !== "undefined") ? {
        open: globe.open,
        lon0: globe.lon0,
        lat0: globe.lat0,
        zoom: globe.zoom,
        spin: !!globe.spin,
      } : null,
      ts: typeof tsDoneJobs === "undefined" ? null : {
        done: tsDoneJobs.slice(0, 8),
        adhoc: adhocTs.ready ? adhocTs.job : null,
        cards: [...document.querySelectorAll(".ts-float")].map((d) => {
          const r = d.getBoundingClientRect();
          return {
            job: d.id.replace("ts-card-", ""),
            left: Math.round(r.left),
            top: Math.round(r.top),
            width: Math.round(r.width),
            height: Math.round(r.height),
          };
        }),
      },
    }));
  } catch { /* storage unavailable — persistence is best-effort */ }
}

function setStatus(msg, isError) {
  statusEl.textContent = msg || "";
  statusEl.classList.toggle("error", !!isError);
}

// ---- flat-map graticule: polyline layers rebuilt when the zoom band
// changes (finer spacing as you zoom in); the globe draws its own lines
// in globeRender from the same gratState.
const gratRenderer = L.canvas({ pane: "borders" });
const gratLatGroup = L.layerGroup();
const gratLonGroup = L.layerGroup();
let gratSpacing = null;

function gratSpacingForZoom(z) {
  return z <= 3 ? 30 : z <= 5 ? 10 : z <= 7 ? 5 : 2;
}

function buildGraticule() {
  const s = gratSpacingForZoom(map.getZoom());
  if (s === gratSpacing) return;
  gratSpacing = s;
  gratLatGroup.clearLayers();
  gratLonGroup.clearLayers();
  const style = {
    renderer: gratRenderer,
    pane: "borders",
    interactive: false,
    color: "#ffffff",
    weight: 1,
    opacity: 0.15,
  };
  const latMax = s === 30 ? 60 : 80;
  for (let lat = -latMax; lat <= latMax; lat += s) {
    gratLatGroup.addLayer(L.polyline([[lat, -900], [lat, 900]], style));
  }
  for (let lon = -900; lon <= 900; lon += s) {
    gratLonGroup.addLayer(L.polyline([[-85, lon], [85, lon]], style));
  }
}

function applyGraticule() {
  buildGraticule();
  gratState.lat ? gratLatGroup.addTo(map) : map.removeLayer(gratLatGroup);
  gratState.lon ? gratLonGroup.addTo(map) : map.removeLayer(gratLonGroup);
  updateGratLabels();
}

// degree formatting shared by the map edge labels and the globe labels
function fmtLat(v) {
  return v === 0 ? "0°" : `${Math.abs(v)}°${v > 0 ? "N" : "S"}`;
}
function fmtLon(v) {
  const w = ((v + 180) % 360 + 360) % 360 - 180;
  if (w === 0) return "0°";
  if (w === 180 || w === -180) return "180°";
  return `${Math.abs(w)}°${w > 0 ? "E" : "W"}`;
}

// Coordinate labels for the flat map: latitudes down the left edge,
// longitudes along the bottom edge, tracking pan/zoom continuously.
const gratLabelHost = document.createElement("div");
gratLabelHost.className = "grat-labels";
map.getContainer().appendChild(gratLabelHost);

function updateGratLabels() {
  gratLabelHost.innerHTML = "";
  if (!gratState.lat && !gratState.lon) return;
  const s = gratSpacing || gratSpacingForZoom(map.getZoom());
  const b = map.getBounds();
  const size = map.getSize();
  const frag = document.createDocumentFragment();
  const add = (txt, left, top, rightEdge) => {
    const span = document.createElement("span");
    span.textContent = txt;
    if (rightEdge) span.style.right = "6px";
    else span.style.left = `${Math.round(left)}px`;
    span.style.top = `${Math.round(top)}px`;
    frag.appendChild(span);
  };
  if (gratState.lat) {
    // right edge — the side panel hides the left edge
    const latMax = s === 30 ? 60 : 80;
    const lo = Math.max(-latMax, Math.ceil(b.getSouth() / s) * s);
    const hi = Math.min(latMax, Math.floor(b.getNorth() / s) * s);
    for (let lat = lo; lat <= hi; lat += s) {
      const y = map.latLngToContainerPoint([lat, b.getCenter().lng]).y;
      if (y > 10 && y < size.y - 10) add(fmtLat(lat), 0, y - 10, true);
    }
  }
  if (gratState.lon) {
    // top edge — the play bar and timeline cover the bottom of the window
    const lo = Math.ceil(b.getWest() / s) * s;
    const hi = Math.floor(b.getEast() / s) * s;
    for (let lon = lo; lon <= hi; lon += s) {
      const x = map.latLngToContainerPoint([0, lon]).x;
      if (x > 8 && x < size.x - 44) add(fmtLon(lon), x + 5, 8);
    }
  }
  gratLabelHost.appendChild(frag);
}

map.on("move zoom viewreset resize", updateGratLabels);
map.on("zoomend", () => {
  const before = gratSpacing;
  buildGraticule();
  if (gratSpacing !== before) applyGraticule();
  updateGratLabels();
});

el("grat-lat").checked = gratState.lat;
el("grat-lon").checked = gratState.lon;
applyGraticule();
for (const axis of ["lat", "lon"]) {
  el(`grat-${axis}`).addEventListener("change", (e) => {
    gratState[axis] = e.target.checked;
    applyGraticule();
    if (typeof globe !== "undefined" && globe.open) globeRender(false);
    saveState();
  });
}

// -------------------------------------------------------- field sampling
function toFloat32(rows, nlat, nlon) {
  const a = new Float32Array(nlat * nlon);
  for (let i = 0; i < nlat; i++) {
    const row = rows[i];
    const base = i * nlon;
    for (let j = 0; j < nlon; j++) {
      const v = row[j];
      a[base + j] = v == null ? NaN : v;
    }
  }
  return a;
}

// Bilinear sample of a flat [nlat*nlon] Float32Array at (lat, lon);
// wraps across the antimeridian. g holds grid geometry.
function sampleGrid(g, arr, lat, lon) {
  const x = ((((lon - g.lon0) / g.dlon) % g.nlon) + g.nlon) % g.nlon;
  const y = (lat - g.lat0) / g.dlat;
  if (y < 0 || y > g.nlat - 1) return null;
  const x0 = x | 0, y0 = y | 0;
  const x1 = (x0 + 1) % g.nlon, y1 = Math.min(y0 + 1, g.nlat - 1);
  const tx = x - x0, ty = y - y0;
  const n = g.nlon;
  const v00 = arr[y0 * n + x0], v10 = arr[y0 * n + x1];
  const v01 = arr[y1 * n + x0], v11 = arr[y1 * n + x1];
  if (Number.isNaN(v00) || Number.isNaN(v10) || Number.isNaN(v01) || Number.isNaN(v11)) {
    const vn = arr[(ty > 0.5 ? y1 : y0) * n + (tx > 0.5 ? x1 : x0)];
    return Number.isNaN(vn) ? null : vn;
  }
  return (
    v00 * (1 - tx) * (1 - ty) +
    v10 * tx * (1 - ty) +
    v01 * (1 - tx) * ty +
    v11 * tx * ty
  );
}

function nearestCell(g, lat, lon) {
  let i = Math.round((lat - g.lat0) / g.dlat);
  let j = Math.round((lon - g.lon0) / g.dlon);
  i = Math.max(0, Math.min(g.nlat - 1, i));
  j = ((j % g.nlon) + g.nlon) % g.nlon;
  const v = g._grid[i * g.nlon + j];
  return {
    value: Number.isNaN(v) ? null : v,
    lat: g.lat0 + i * g.dlat,
    lon: g.lon0 + j * g.dlon,
  };
}

// -------------------------------------------------------- raster rendering
// Draw the field into a canvas whose rows are spaced in Web Mercator y,
// so a plain imageOverlay spanning [-85.05, 85.05] lines up with the tiles.
function renderFieldToURL(d, lut, filter) {
  const grid = d._grid;
  const fLo = filter ? filter.lo : null;
  const fHi = filter ? filter.hi : null;
  const { nlat, nlon, lat0, dlat, lon0, dlon } = d;
  const W = 1600, H = 1250;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(W, H);
  const px = img.data;

  // Column -> grid x mapping is independent of the row; precompute it.
  const cx0 = new Int32Array(W), cx1 = new Int32Array(W);
  const ctx_ = new Float32Array(W);
  for (let c = 0; c < W; c++) {
    const lon = -180 + (360 * c) / (W - 1);
    const x = ((((lon - lon0) / dlon) % nlon) + nlon) % nlon;
    cx0[c] = x | 0;
    cx1[c] = (cx0[c] + 1) % nlon;
    ctx_[c] = x - cx0[c];
  }

  const yMax = Math.log(Math.tan(Math.PI / 4 + (MERC_LAT * Math.PI) / 360));
  for (let r = 0; r < H; r++) {
    // row 0 = top of image = +85.05; invert Mercator to get latitude
    const my = yMax - (2 * yMax * r) / (H - 1);
    const lat = (2 * Math.atan(Math.exp(my)) - Math.PI / 2) * (180 / Math.PI);
    const y = (lat - lat0) / dlat;
    if (y < 0 || y > nlat - 1) continue; // leave the row transparent
    const y0 = y | 0;
    const y1 = Math.min(y0 + 1, nlat - 1);
    const ty = y - y0;
    const row0 = y0 * nlon, row1 = y1 * nlon;
    let k = r * W * 4;
    for (let c = 0; c < W; c++, k += 4) {
      const tx = ctx_[c];
      const v00 = grid[row0 + cx0[c]], v10 = grid[row0 + cx1[c]];
      const v01 = grid[row1 + cx0[c]], v11 = grid[row1 + cx1[c]];
      let v =
        v00 * (1 - tx) * (1 - ty) +
        v10 * tx * (1 - ty) +
        v01 * (1 - tx) * ty +
        v11 * tx * ty;
      if (Number.isNaN(v)) {
        v = grid[(ty > 0.5 ? row1 : row0) + (tx > 0.5 ? cx1[c] : cx0[c])];
        if (Number.isNaN(v)) continue;
      }
      if (fLo != null && v < fLo) continue; // filtered out -> transparent
      if (fHi != null && v > fHi) continue;
      let idx = ((v - lut.min) * lut.scale) | 0;
      if (idx < 0) idx = 0;
      else if (idx > lut.last) idx = lut.last;
      const li = idx * 4;
      px[k] = lut.data[li];
      px[k + 1] = lut.data[li + 1];
      px[k + 2] = lut.data[li + 2];
      px[k + 3] = lut.data[li + 3];
    }
  }
  ctx.putImageData(img, 0, 0);

  // toBlob encodes the PNG off the UI thread (toDataURL blocks it for
  // hundreds of ms, which froze the selects on every switch).
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob ? URL.createObjectURL(blob) : null), "image/png");
  });
}

function applyOverlayUrl(url) {
  const opacity = el("opacity").value / 100;
  if (overlays.length) {
    for (const o of overlays) {
      o.setUrl(url);
      o.setOpacity(opacity);
    }
  } else {
    overlays = WORLD_COPIES.map((offset) =>
      L.imageOverlay(url, [[-MERC_LAT, -180 + offset], [MERC_LAT, 180 + offset]], {
        opacity,
        interactive: false,
      }).addTo(map)
    );
  }
}

function renderOverlay(d, lut, seq, filter) {
  renderFieldToURL(d, lut, filter).then((url) => {
    if (!url) return;
    if (seq !== loadSeq) {
      URL.revokeObjectURL(url); // superseded by a newer selection
      return;
    }
    applyOverlayUrl(url);
    if (overlayUrl) URL.revokeObjectURL(overlayUrl);
    overlayUrl = url;
  });
}

// Legend value range in BASE units, kept for the filter handle math.
let legendRange = { min: 0, max: 1 };

// spec: {min, max, ticks, color(v, out), label, units}
function renderLegend(spec) {
  legendRange = { min: spec.min, max: spec.max };
  const canvas = el("legend-canvas");
  const ctx = canvas.getContext("2d");
  const { width: W, height: H } = canvas;
  const img = ctx.createImageData(W, H);
  const rgba = [0, 0, 0, 0];
  for (let c = 0; c < W; c++) {
    const v = spec.min + ((spec.max - spec.min) * c) / (W - 1);
    spec.color(v, rgba);
    for (let r = 0; r < H; r++) {
      const k = (r * W + c) * 4;
      img.data[k] = rgba[0]; img.data[k + 1] = rgba[1];
      img.data[k + 2] = rgba[2]; img.data[k + 3] = rgba[3];
    }
  }
  ctx.clearRect(0, 0, W, H);
  ctx.putImageData(img, 0, 0);
  el("legend-ticks").innerHTML = spec.ticks.map((t) => `<span>${t}</span>`).join("");
  el("legend-units").textContent = `${spec.label} (${spec.units})`;
  updateFilterUI();
}

// -------------------------------------------------------- value filter
// state.filter holds base-unit bounds; the legend handles and the numeric
// inputs are two synchronized views of it.
function filterSpec() {
  return unitSpecFor(currentDisplay ? currentDisplay.var : state.var);
}

function toBaseUnits(displayVal, spec) {
  return isDeltaLike(currentDisplay)
    ? displayVal / spec.scale
    : (displayVal - spec.offset) / spec.scale;
}

function toDisplayUnits(baseVal, spec) {
  return convForDisplay(currentDisplay, baseVal, spec);
}

function updateFilterUI() {
  const { min, max } = legendRange;
  const span = max - min || 1;
  const fracLo = state.filter.lo == null ? 0 : Math.min(1, Math.max(0, (state.filter.lo - min) / span));
  const fracHi = state.filter.hi == null ? 1 : Math.min(1, Math.max(0, (state.filter.hi - min) / span));
  el("legend-handle-lo").style.left = `${fracLo * 100}%`;
  el("legend-handle-hi").style.left = `${fracHi * 100}%`;
  el("legend-dim-lo").style.left = "0";
  el("legend-dim-lo").style.width = `${fracLo * 100}%`;
  el("legend-dim-hi").style.left = `${fracHi * 100}%`;
  el("legend-dim-hi").style.width = `${(1 - fracHi) * 100}%`;
  const spec = filterSpec();
  if (document.activeElement !== el("filter-lo")) {
    el("filter-lo").value =
      state.filter.lo == null ? "" : +toDisplayUnits(state.filter.lo, spec).toPrecision(4);
  }
  if (document.activeElement !== el("filter-hi")) {
    el("filter-hi").value =
      state.filter.hi == null ? "" : +toDisplayUnits(state.filter.hi, spec).toPrecision(4);
  }
}

let filterRenderTimer = null;
function scheduleFilterRender(immediate) {
  if (filterRenderTimer) return;
  filterRenderTimer = setTimeout(() => {
    filterRenderTimer = null;
    if (currentDisplay && currentLUT) {
      renderOverlay(currentDisplay, currentLUT, loadSeq, state.filter);
    }
  }, immediate ? 0 : 250);
}

function setFilter(lo, hi, immediate) {
  // keep the bounds ordered when both are set
  if (lo != null && hi != null && lo > hi) [lo, hi] = [hi, lo];
  state.filter.lo = lo;
  state.filter.hi = hi;
  updateFilterUI();
  scheduleFilterRender(immediate);
}

function initFilterControls() {
  const bar = el("legend-bar");
  for (const [id, which] of [["legend-handle-lo", "lo"], ["legend-handle-hi", "hi"]]) {
    el(id).addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const move = (ev) => {
        const r = bar.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
        const val = legendRange.min + frac * (legendRange.max - legendRange.min);
        if (which === "lo") {
          setFilter(frac <= 0.001 ? null : val, state.filter.hi);
        } else {
          setFilter(state.filter.lo, frac >= 0.999 ? null : val);
        }
      };
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        scheduleFilterRender(true);
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }
  const readInputs = () => {
    const spec = filterSpec();
    const lo = el("filter-lo").value.trim();
    const hi = el("filter-hi").value.trim();
    setFilter(
      lo === "" ? null : toBaseUnits(parseFloat(lo), spec),
      hi === "" ? null : toBaseUnits(parseFloat(hi), spec),
      true
    );
  };
  el("filter-lo").addEventListener("change", readInputs);
  el("filter-hi").addEventListener("change", readInputs);
  el("filter-clear").addEventListener("click", () => setFilter(null, null, true));
}

// -------------------------------------------------------- wind particles
const particles = {
  canvas: null,
  ctx: null,
  list: [],
  raf: null,
  lastT: 0,
  paused: false,
};

const PARTICLE_SPEED = 0.0075; // degrees lon/lat per (m/s) per 60fps frame
const PARTICLE_MAX_AGE = 120;  // frames

// Zoomed in, a constant per-pixel particle density is visually far too
// busy, and a constant degrees-per-frame speed turns into racing streaks.
// Scale both down as zoom increases (zoom 3 is the reference look).
function zoomDensityFactor() {
  return Math.min(1, Math.max(0.1, Math.pow(2, -(map.getZoom() - 3))));
}
function zoomSpeedFactor() {
  return Math.min(1.6, Math.max(0.12, Math.pow(2, (3 - map.getZoom()) * 0.7)));
}

function particlesEnabled() {
  return !state.compare && el("particles-toggle").checked && currentWind != null;
}

function initParticleCanvas() {
  const canvas = document.createElement("canvas");
  canvas.className = "particle-canvas";
  map.getPane("particles").appendChild(canvas);
  particles.canvas = canvas;
  particles.ctx = canvas.getContext("2d");
  positionParticleCanvas();

  // While the map is panning or zooming, screen positions computed from
  // lat/lon change for reasons that aren't wind — drawing segments then
  // produces "warp speed" streaks. Pause and re-seed instead.
  map.on("movestart zoomstart", () => {
    particles.paused = true;
    clearParticleCanvas();
  });
  map.on("moveend zoomend resize", () => {
    positionParticleCanvas();
    if (particlesEnabled()) resetParticles();
    particles.paused = false;
  });
}

function positionParticleCanvas() {
  const size = map.getSize();
  const canvas = particles.canvas;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== size.x * dpr || canvas.height !== size.y * dpr) {
    // render at device resolution for crisp trails on scaled displays
    canvas.width = size.x * dpr;
    canvas.height = size.y * dpr;
    canvas.style.width = `${size.x}px`;
    canvas.style.height = `${size.y}px`;
    particles.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  // The pane is translated as the map pans; pin the canvas to the viewport.
  L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));
}

function clearParticleCanvas() {
  if (particles.ctx) {
    particles.ctx.clearRect(0, 0, particles.canvas.width, particles.canvas.height);
  }
}

function spawnParticle(p) {
  const size = map.getSize();
  const pt = [Math.random() * size.x, Math.random() * size.y];
  const ll = map.containerPointToLatLng(pt);
  p.lat = ll.lat;
  p.lon = ll.lng;
  p.px = pt[0];
  p.py = pt[1];
  p.age = Math.floor(Math.random() * PARTICLE_MAX_AGE);
  return p;
}

function resetParticles() {
  clearParticleCanvas();
  const size = map.getSize();
  const n = Math.max(300, Math.round(((size.x * size.y) / 400) * zoomDensityFactor()));
  particles.list = Array.from({ length: n }, () => spawnParticle({}));
}

function stepParticles(now) {
  particles.raf = requestAnimationFrame(stepParticles);
  if (now - particles.lastT < 30) return; // ~30fps cap; halves the canvas work
  const dtf = Math.min(3, Math.max(0.5, (now - particles.lastT) / 16.7)); // frames elapsed
  particles.lastT = now;
  if (particles.paused || !particlesEnabled()) return;

  const w = currentWind;
  const ctx = particles.ctx;
  const size = map.getSize();
  const speed = PARTICLE_SPEED * zoomSpeedFactor();

  // Fade previous frame to leave dissolving trails.
  ctx.globalCompositeOperation = "destination-in";
  ctx.fillStyle = "rgba(0, 0, 0, 0.95)";
  ctx.fillRect(0, 0, size.x, size.y);
  ctx.globalCompositeOperation = "source-over";
  ctx.strokeStyle = "rgba(235, 240, 245, 0.65)";
  ctx.lineWidth = 1.1;
  ctx.beginPath();

  for (const p of particles.list) {
    p.age += dtf;
    const u = sampleGrid(w, w._u, p.lat, p.lon);
    const v = sampleGrid(w, w._v, p.lat, p.lon);
    if (u == null || v == null || p.age > PARTICLE_MAX_AGE) {
      spawnParticle(p);
      continue;
    }
    p.lon += (u * speed * dtf) / Math.max(Math.cos((p.lat * Math.PI) / 180), 0.05);
    p.lat += v * speed * dtf;
    if (p.lat > MERC_LAT || p.lat < -MERC_LAT) { spawnParticle(p); continue; }
    const pt = map.latLngToContainerPoint([p.lat, p.lon]);
    if (pt.x < -20 || pt.x > size.x + 20 || pt.y < -20 || pt.y > size.y + 20) {
      spawnParticle(p);
      continue;
    }
    ctx.moveTo(p.px, p.py);
    ctx.lineTo(pt.x, pt.y);
    p.px = pt.x;
    p.py = pt.y;
  }
  ctx.stroke();
}

function startParticles() {
  if (particles.raf == null) {
    particles.lastT = performance.now();
    particles.raf = requestAnimationFrame(stepParticles);
  }
}

function stopParticles() {
  if (particles.raf != null) {
    cancelAnimationFrame(particles.raf);
    particles.raf = null;
  }
  clearParticleCanvas();
}

// -------------------------------------------------------- click readout
function windReadout(lat, lon) {
  if (!currentWind) return "";
  const u = sampleGrid(currentWind, currentWind._u, lat, lon);
  const v = sampleGrid(currentWind, currentWind._v, lat, lon);
  if (u == null || v == null) return "";
  const spec = unitSpecFor("sfcWind");
  const speed = convVal(Math.hypot(u, v), spec);
  const dirFrom = (270 - (Math.atan2(v, u) * 180) / Math.PI + 360) % 360;
  const levTag = currentWind.plev ? ` (${currentWind.plev} hPa)` : " (10 m)";
  return `<br>wind${levTag} ${speed.toFixed(spec.decimals)} ${spec.unit} from ${Math.round(dirFrom)}&deg;`;
}

const readoutPopup = L.popup({ autoPan: false });
let lastClickLatLng = null; // the ad-hoc (non-station) popup's location

function selLabel(f) {
  const stat = f.stat || (f.member === "ensmean" ? "mean" : "raw");
  const m =
    stat === "mean" ? "ens mean" :
    stat === "spread" ? "ens spread" :
    stat === "anom" ? `${f.member} − mean` :
    f.member === "ensmean" ? "ens mean" : f.member;
  return `${m} · ${f.time}` + (f.plev ? ` · ${f.plev} hPa` : "");
}

// idx: station index (0-based) for pin cards, -1 for the ad-hoc popup.
function readoutContent(latlng, withTs, idx = -1) {
  const d = currentDisplay;
  const lon = ((latlng.lng + 180) % 360 + 360) % 360 - 180;
  const cell = nearestCell(d, latlng.lat, lon);
  const spec = unitSpecFor(d.var);
  const shown = cell.value == null ? null : convForDisplay(d, cell.value, spec);
  const val =
    shown == null
      ? "no data"
      : `<span class="popup-value">${isDeltaLike(d) && shown > 0 ? "+" : ""}${fmtVal(shown, spec)} ${spec.unit}</span>`;
  let details;
  if (d.isDiff) {
    const a = nearestCell(currentField, latlng.lat, lon).value;
    const b = nearestCell(currentFieldB, latlng.lat, lon).value;
    details =
      `${d.label}` +
      `<br>A: ${a == null ? "–" : fmtVal(convVal(a, spec), spec)} (${selLabel(currentField)})` +
      `<br>B: ${b == null ? "–" : fmtVal(convVal(b, spec), spec)} (${selLabel(currentFieldB)})`;
  } else {
    details = `${statPrefix(d)}${d.label} · ${selLabel(d)}` + windReadout(latlng.lat, lon);
  }
  const latStr = `${Math.abs(cell.lat).toFixed(2)}&deg;${cell.lat >= 0 ? "N" : "S"}`;
  const lonStr = `${Math.abs(cell.lon).toFixed(2)}&deg;${cell.lon >= 0 ? "E" : "W"}`;
  let coordLine;
  if (withTs && coordEditIdx === idx) {
    // unlocked: manual coordinate entry with a submit button
    const lon180 = ((latlng.lng + 180) % 360 + 360) % 360 - 180;
    coordLine =
      `<span class="coord-edit">lat <input class="coord-lat" type="number" step="0.01" ` +
      `min="-85" max="85" value="${latlng.lat.toFixed(2)}" onclick="event.stopPropagation()" /> ` +
      `lon <input class="coord-lon" type="number" step="0.01" min="-180" max="180" ` +
      `value="${lon180.toFixed(2)}" onclick="event.stopPropagation()" /> ` +
      `<button type="button" class="coord-submit" onclick="coordSubmitFor(${idx}, event)">Set</button></span> ` +
      `<span class="coord-lock" onclick="coordLockFor(${idx}, event)" title="Lock coordinates">&#128275;</span>`;
  } else {
    coordLine =
      `grid cell ${latStr}, ${lonStr}` +
      (withTs
        ? ` <span class="coord-lock" onclick="coordLockFor(${idx}, event)" title="Unlock to edit coordinates">&#128274;</span>`
        : "");
  }
  const inner =
    `${val}<br><span class="popup-coords">${details}` +
    `<br>${coordLine}</span>` +
    (withTs ? tsSectionHTML(idx) : "");
  // Station cards are interactive — swallow events so clicks inside them
  // never reach the map (which would place stations / move the popup).
  return withTs
    ? `<div onclick="event.stopPropagation()" onmousedown="event.stopPropagation()" ` +
      `ondblclick="event.stopPropagation()">${inner}</div>`
    : inner;
}

// ---- coordinate lock/unlock (per station card / popup)
let coordEditIdx = null; // null = all locked; -1 = ad-hoc popup; >=0 station

function coordLockFor(idx, ev) {
  ev.stopPropagation();
  coordEditIdx = coordEditIdx === idx ? null : idx;
  refreshReadout();
}

function coordSubmitFor(idx, ev) {
  ev.stopPropagation();
  // relative lookup: the same card markup can exist on the map AND the globe
  const box = ev.target.closest(".coord-edit");
  const lat = parseFloat(box.querySelector(".coord-lat").value);
  const lonRaw = parseFloat(box.querySelector(".coord-lon").value);
  if (!Number.isFinite(lat) || !Number.isFinite(lonRaw) || lat < -85 || lat > 85) {
    setStatus("Coordinates: latitude must be between −85 and 85", true);
    return;
  }
  const ll = L.latLng(lat, ((lonRaw + 180) % 360 + 360) % 360 - 180);
  if (idx >= 0 && pinnedPoints[idx]) {
    const p = pinnedPoints[idx];
    p.latlng = ll;
    p.marker.setLatLng(ll);
    // an extraction made at the old position no longer applies
    p.tsJob = null;
    p.tsReady = false;
  } else {
    lastClickLatLng = ll;
    adhocTs = { job: null, ready: false };
    readoutPopup.setLatLng(ll);
  }
  coordEditIdx = null;
  refreshReadout();
  updateMergeButton();
  saveState();
}

// While the popup stays open, loadField() refreshes it after every
// parameter change so the readout always matches the displayed field.
function refreshReadout() {
  if (lastClickLatLng && readoutPopup.isOpen()) {
    readoutPopup.setContent(readoutContent(lastClickLatLng, true, -1));
  }
  refreshPins();
}

// ---- pinned multi-points (Ctrl/Cmd+click, up to MAX_PINNED)
const MAX_PINNED = 9;
const pinnedPoints = []; // {latlng, marker}

// Station colors — must match TS_PALETTE in server.py so pin badges,
// per-station plots and merged plots all agree.
const STATION_PALETTE = ["#3987e5", "#e8833a", "#2fbf71", "#e0575b", "#a06ee0",
                         "#e5c43a", "#56c8d8", "#e06ea8", "#96a84c"];
function stationColor(n) {
  return n > 0 ? STATION_PALETTE[(n - 1) % STATION_PALETTE.length] : "#cfcfc8";
}

function pinIcon(n) {
  // integer-sized icon with a centered anchor — avoids the half-pixel CSS
  // transform that blurred badge text on scaled displays
  return L.divIcon({
    className: "pin-anchor",
    html: `<span class="pin-badge" style="background:${stationColor(n)}">${n}</span>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}

function pinCompactValue(latlng) {
  if (!currentDisplay) return "";
  const d = currentDisplay;
  const lon = ((latlng.lng + 180) % 360 + 360) % 360 - 180;
  const cell = nearestCell(d, latlng.lat, lon);
  if (cell.value == null) return "no data";
  const spec = unitSpecFor(d.var);
  const v = convForDisplay(d, cell.value, spec);
  return `${isDeltaLike(d) && v > 0 ? "+" : ""}${fmtVal(v, spec)} ${spec.unit}`;
}

function pinToggleCollapse(i, ev) {
  ev.stopPropagation();
  if (pinnedPoints[i]) {
    pinnedPoints[i].collapsed = !pinnedPoints[i].collapsed;
    refreshPins();
    saveState();
  }
}

function refreshPins() {
  pinnedPoints.forEach((p, i) => {
    p.marker.setIcon(pinIcon(i + 1));
    const head =
      `<span class="pin-num">#${i + 1}</span>` +
      `<span class="pin-collapse" onclick="pinToggleCollapse(${i}, event)" ` +
      `title="${p.collapsed ? "Expand" : "Collapse"}">${p.collapsed ? "&#9656;" : "&#9662;"}</span>`;
    // Full-featured station card (readout, time series, coordinate lock),
    // individually collapsible to a compact number+value chip.
    p.marker.setTooltipContent(
      !currentDisplay
        ? `#${i + 1}`
        : p.collapsed
          ? `<div onclick="event.stopPropagation()">${head} ${pinCompactValue(p.latlng)}</div>`
          : `<div onclick="event.stopPropagation()">${head}</div>${readoutContent(p.latlng, true, i)}`
    );
  });
  const btn = el("clear-pins");
  btn.hidden = pinnedPoints.length === 0;
  if (pinnedPoints.length) {
    btn.innerHTML = `&#10005;&nbsp;Clear all points (${pinnedPoints.length})`;
  }
  updateMergeButton();
  if (typeof globe !== "undefined" && globe.open) globeSyncPinContent();
}

el("clear-pins").addEventListener("click", () => {
  for (const p of pinnedPoints) map.removeLayer(p.marker);
  pinnedPoints.length = 0;
  refreshPins();
  saveState();
});

function removePin(p) {
  map.removeLayer(p.marker);
  pinnedPoints.splice(pinnedPoints.indexOf(p), 1);
  refreshPins();
  saveState();
}

function addPinnedPoint(latlng) {
  // Ctrl+click near an existing pin toggles it off instead
  const at = map.latLngToContainerPoint(latlng);
  for (const p of pinnedPoints) {
    const q = map.latLngToContainerPoint(p.latlng);
    if (Math.hypot(at.x - q.x, at.y - q.y) < 16) {
      removePin(p);
      return;
    }
  }
  createPin(latlng);
}

// Marker/tooltip creation shared by the flat map and the globe (which does
// its own screen-space proximity check before calling this).
function createPin(latlng) {
  if (pinnedPoints.length >= MAX_PINNED) {
    setStatus(`Up to ${MAX_PINNED} pinned points — click a pin to remove it`, true);
    return;
  }
  const marker = L.marker(latlng, { icon: pinIcon(pinnedPoints.length + 1), keyboard: false }).addTo(map);
  marker.bindTooltip("", {
    permanent: true,
    direction: "top",
    offset: [0, -12],
    className: "pin-tip",
  });
  const p = { latlng, marker, tsJob: null, tsReady: false };
  // The station card is always visible next to the pin; Ctrl+click the
  // badge (or the card's "remove" link) removes the station.
  marker.on("click", (ev) => {
    if (ev.originalEvent && (ev.originalEvent.ctrlKey || ev.originalEvent.metaKey)) {
      removePin(p);
    }
  });
  pinnedPoints.push(p);
  refreshPins();
  saveState();
}

map.on("click", (e) => {
  if (!currentDisplay) return;
  if (e.originalEvent && (e.originalEvent.ctrlKey || e.originalEvent.metaKey)) {
    addPinnedPoint(e.latlng);
    return;
  }
  // The very first placed point always becomes station 1, Ctrl or not.
  if (pinnedPoints.length === 0) {
    addPinnedPoint(e.latlng);
    return;
  }
  // Once stations exist, plain clicks are inert: further stations are
  // Ctrl+click only. (The old un-numbered ad-hoc readout popup looked
  // like a rogue station once it grew the time-series section.)
});

// -------------------------------------------------------- data loading
async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail;
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function fetchField(sel) {
  const p = { var: state.var, ...sel };
  if (meta.variables[state.var].plev) p.plev = state.plev;
  return fetchJSON(`/api/field?${new URLSearchParams(p)}`);
}

// Full-screen overlay for heavy loads (pressure-level ensemble means).
// True progress isn't knowable from one HTTP request, so the percentage
// is an asymptotic estimate calibrated by the last observed heavy-load
// duration (remembered in localStorage), capped at 95% until completion.
const heavyOverlay = { timer: null, delay: null, t0: 0, seq: 0, kind: "sfc" };

// Separate calibration per load class: pressure-level means move ~230 MB,
// surface/ocean means only ~25 MB, so their percentages run on different
// clocks (each adapts to observed durations on this connection).
const HEAVY_DEFAULT_SECS = { plev: 120, sfc: 12 };
const HEAVY_NOTES = {
  plev: "Reading all 30 members on pressure levels (~230&nbsp;MB from the cloud store).<br>First load takes a while — this selection and its sibling levels will be instant afterwards.",
  sfc: "Reading all 30 members (~25&nbsp;MB from the cloud store).<br>First load only — this selection will be instant afterwards.",
};

// Name the statistic being computed in the overlay title.
function heavyStatLabel() {
  const names = { mean: "ensemble mean", spread: "ensemble spread", anom: "ensemble anomaly" };
  const active = new Set();
  if (state.stat !== "raw") active.add(state.stat);
  if (state.compare && state.b.stat !== "raw") active.add(state.b.stat);
  if (active.size === 1) return names[[...active][0]];
  return "ensemble statistics";
}

function showHeavyOverlay(seq, kind) {
  heavyOverlay.seq = seq;
  heavyOverlay.kind = kind;
  heavyOverlay.t0 = performance.now();
  el("load-overlay").hidden = false;
  el("load-title").textContent = `Loading ${heavyStatLabel()}…`;
  el("load-note").innerHTML = HEAVY_NOTES[kind];
  const est = Math.max(
    4, +(localStorage.getItem(`heavyLoadSecs_${kind}`) || HEAVY_DEFAULT_SECS[kind])
  );
  el("load-pct").textContent = "0%";
  heavyOverlay.timer = setInterval(() => {
    const t = (performance.now() - heavyOverlay.t0) / 1000;
    const pct = Math.min(95, Math.round(100 * (1 - Math.exp(-t / (est / 2.5)))));
    el("load-pct").textContent = `${pct}%`;
  }, 250);
}

// Percent-free overlay for plain (non-statistic) loads: a raw field can
// still take seconds on a cache miss, and a greyed screen with a spinner
// beats looking frozen. Shares the heavyOverlay delay/seq machinery.
function showPlainOverlay(seq) {
  heavyOverlay.seq = seq;
  heavyOverlay.kind = null; // no percent estimate, no EMA timing update
  heavyOverlay.t0 = performance.now();
  el("load-overlay").hidden = false;
  el("load-title").textContent = `Loading ${meta.variables[state.var].long}…`;
  el("load-pct").textContent = "";
  el("load-note").innerHTML =
    "Fetching this selection from the cloud data store.<br>" +
    "Repeat selections are cached and load instantly.";
}

function hideHeavyOverlay(seq, success) {
  if (heavyOverlay.seq !== seq) return; // a newer load owns the overlay
  clearTimeout(heavyOverlay.delay);
  heavyOverlay.delay = null;
  if (heavyOverlay.timer) {
    clearInterval(heavyOverlay.timer);
    heavyOverlay.timer = null;
  }
  if (!el("load-overlay").hidden) {
    const secs = (performance.now() - heavyOverlay.t0) / 1000;
    if (success && secs > 2 && heavyOverlay.kind) {
      // exponential moving average keeps the estimate adaptive
      const key = `heavyLoadSecs_${heavyOverlay.kind}`;
      const prev = +(localStorage.getItem(key) || secs);
      localStorage.setItem(key, String(Math.round(prev * 0.5 + secs * 0.5)));
    }
    el("load-overlay").hidden = true;
  }
}

async function loadField() {
  if (typeof playback !== "undefined" && (playback.playing || playback.frames)) {
    discardPlayback(); // any selection change invalidates the loaded year
  }
  const seq = ++loadSeq;
  saveState();
  const selA = {
    experiment: state.experiment,
    member: state.member,
    stat: state.stat,
    time: el("month-input").value,
  };
  setStatus("Loading field…");
  // Elapsed-time ticker: first-time ensemble statistics on pressure levels
  // pull ~30x more data (hundreds of MB) and can take minutes — show life.
  const t0 = performance.now();
  const heavy = state.stat !== "raw" || (state.compare && state.b.stat !== "raw");
  const heavyKind = meta.variables[state.var].plev ? "plev" : "sfc";
  const loadTimer = setInterval(() => {
    if (seq !== loadSeq) return;
    const s = Math.round((performance.now() - t0) / 1000);
    setStatus(
      `Loading field… ${s}s` +
      (heavy && heavyKind === "plev" && s > 5
        ? " — ensemble means on pressure levels move ~230 MB on first load; this can take a few minutes"
        : "")
    );
  }, 1000);
  // Grey out the whole UI with the progress overlay, but only if the load
  // is actually slow (a cache hit finishes before the 700 ms delay fires).
  let ok = false;
  if (heavy) {
    heavyOverlay.delay = setTimeout(() => showHeavyOverlay(seq, heavyKind), 700);
  } else {
    // plain loads get a percent-free overlay if they turn out slow
    heavyOverlay.delay = setTimeout(() => showPlainOverlay(seq), 700);
  }
  heavyOverlay.seq = seq;
  try {
    if (state.compare) {
      const selB = {
        experiment: state.b.experiment,
        member: state.b.member,
        stat: state.b.stat,
        time: el("month-input-b").value,
      };
      const [fa, fb] = await Promise.all([fetchField(selA), fetchField(selB)]);
      if (seq !== loadSeq) return;
      fa._grid = toFloat32(fa.values, fa.nlat, fa.nlon);
      fb._grid = toFloat32(fb.values, fb.nlat, fb.nlon);
      const diff = new Float32Array(fa._grid.length);
      for (let i = 0; i < diff.length; i++) diff[i] = fa._grid[i] - fb._grid[i];
      const limit = diffLimit(diff);
      currentField = fa;
      currentFieldB = fb;
      currentWind = null;
      stopParticles();
      currentDisplay = {
        isDiff: true,
        limit,
        var: fa.var,
        units: fa.units,
        label: `Δ ${fa.label} (A−B)`,
        nlat: fa.nlat, nlon: fa.nlon,
        lat0: fa.lat0, dlat: fa.dlat, lon0: fa.lon0, dlon: fa.dlon,
        _grid: diff,
      };
      // Let the checkbox/controls repaint before the heavy render.
      await new Promise((r) => setTimeout(r, 0));
      if (seq !== loadSeq) return;
      currentLUT = buildDiffLUT(limit);
      renderOverlay(currentDisplay, currentLUT, seq, state.filter);
      renderMeta();
      ok = true;
    } else {
      // Particle wind follows the viewed level: ua/va at state.plev for
      // pressure-level variables, uas/vas near-surface otherwise. For
      // ensemble statistics the particles show the ensemble-mean wind.
      const windParams = {
        experiment: state.experiment,
        member: state.stat === "raw" ? state.member : "ensmean",
        time: el("month-input").value,
      };
      if (meta.variables[state.var].plev) windParams.plev = state.plev;
      const [f, w] = await Promise.all([
        fetchField(selA),
        fetchJSON(`/api/wind?${new URLSearchParams(windParams)}`),
      ]);
      if (seq !== loadSeq) return;
      f._grid = toFloat32(f.values, f.nlat, f.nlon);
      w._u = toFloat32(w.u, w.nlat, w.nlon);
      w._v = toFloat32(w.v, w.nlat, w.nlon);
      currentField = f;
      currentFieldB = null;
      currentWind = w;
      currentDisplay = f;
      await new Promise((r) => setTimeout(r, 0));
      if (seq !== loadSeq) return;
      currentLUT = lutForField(f);
      renderOverlay(f, currentLUT, seq, state.filter);
      if (particlesEnabled()) {
        resetParticles();
        startParticles();
      }
      renderMeta();
      ok = true;
    }
  } catch (err) {
    if (seq === loadSeq) setStatus(String(err.message || err), true);
  } finally {
    clearInterval(loadTimer);
    hideHeavyOverlay(seq, ok);
  }
}

// Legend, status line, and open readout — everything unit-dependent.
// Called after each load and whenever the display unit changes (a unit
// switch never refetches or re-renders the raster: colors are keyed to
// physical values, only the numerals change).
function renderMeta() {
  if (!currentDisplay) return;
  const spec = unitSpecFor(currentDisplay.var);
  if (currentDisplay.isDiff) {
    const L = currentDisplay.limit;
    renderLegend({
      min: -L,
      max: L,
      ticks: [-L, -L / 2, 0, L / 2, L].map((t) => fmtTick(convDiff(t, spec))),
      color: (v, out) => diffColor(v, L, out),
      label: currentDisplay.label,
      units: spec.unit,
    });
    setStatus(
      `Δ ${currentField.label} · A(${selLabel(currentField)}) − B(${selLabel(currentFieldB)})` +
      ` · scale ±${fmtTick(convDiff(L, spec))} ${spec.unit}`
    );
  } else {
    const f = currentField;
    const cfg = VAR_CONFIG[f.var];
    let min, max, tickVals, colorFn;
    if (f._dyn) {
      // auto-scaled range computed from the loaded field
      ({ min, max } = f._dyn);
      tickVals = [0, 1, 2, 3, 4].map((i) => min + ((max - min) * i) / 4);
      colorFn = f._dyn.div
        ? (v, out) => diffColor(v, f._dyn.limit, out)
        : (v, out) => rampColor(f._dyn.ramp || cfg.ramp, (v - min) / (max - min), 0.88, out);
    } else {
      min = cfg.legend.min;
      max = cfg.legend.max;
      // Scientific-notation ticks are wide; thin them out to avoid crowding.
      tickVals = spec.sci
        ? cfg.legend.ticks.filter((_, i) => i % 2 === 0)
        : cfg.legend.ticks;
      colorFn = (v, out) => colormap(cfg.stops, v, out);
    }
    renderLegend({
      min,
      max,
      ticks: tickVals.map((t) => fmtTick(convForDisplay(f, t, spec))),
      color: colorFn,
      label: statPrefix(f) + f.label,
      units: spec.unit,
    });
    setStatus(
      `${statPrefix(f)}${f.label} · ${selLabel(f)}` +
      ` · max ${fmtTick(convForDisplay(f, f.vmax, spec))} ${spec.unit}`
    );
  }
  if (typeof updateTimeline === "function") updateTimeline();
  refreshReadout();
  if (typeof globe !== "undefined" && globe.open) globeRender(false);
}

// -------------------------------------------------------- controls
function stepMonth(inputId, delta) {
  const input = el(inputId);
  const [y, m] = input.value.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  const next = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
  if (next < input.min || next > input.max) return;
  input.value = next;
  loadField();
}

function applyExperimentRange(inputId, experiment) {
  const exp = meta.experiments[experiment];
  const input = el(inputId);
  input.min = exp.start;
  input.max = exp.end;
  if (input.value < exp.start || input.value > exp.end) input.value = exp.start;
}

function buildSeg(containerId, entries, current, onPick, infos) {
  const div = el(containerId);
  div.innerHTML = "";
  for (const [value, label] of entries) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.dataset.value = value;
    if (infos && infos[value]) {
      btn.title = infos[value];
      const i = document.createElement("span");
      i.className = "info-i";
      i.textContent = "ⓘ";
      i.addEventListener("click", (e) => {
        e.stopPropagation();
        showInfoPopover(infos[value], e.clientX, e.clientY);
      });
      btn.appendChild(i);
    }
    if (value === current) btn.classList.add("active");
    btn.addEventListener("click", () => {
      if (btn.classList.contains("active")) return;
      div.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      onPick(value);
    });
    div.appendChild(btn);
  }
}

function buildMemberDropdown(btnId, listId, members, getMember, setMember) {
  const btn = el(btnId);
  const list = el(listId);
  list.innerHTML = "";
  for (const m of members) {
    const item = document.createElement("div");
    item.className = "dd-item" + (m === getMember() ? " selected" : "");
    item.textContent = m;
    item.addEventListener("click", () => {
      setMember(m);
      list.querySelectorAll(".dd-item").forEach((i) =>
        i.classList.toggle("selected", i.textContent === m)
      );
      list.hidden = true;
      updateControlStates();
      loadField();
    });
    list.appendChild(item);
  }
  btn.addEventListener("click", (e) => {
    if (btn.disabled) return;
    e.stopPropagation();
    document.querySelectorAll(".dd-list").forEach((l) => {
      if (l !== list) l.hidden = true;
    });
    list.hidden = !list.hidden;
  });
}

document.addEventListener("click", () =>
  document.querySelectorAll(".dd-list").forEach((l) => (l.hidden = true))
);

function rebuildUnitSeg() {
  buildSeg(
    "unit-seg",
    UNITS[state.var].map((u) => [u.id, u.label]),
    state.units[state.var],
    (id) => { state.units[state.var] = id; renderMeta(); }
  );
}

function memberBtnState(btn, stat, member) {
  // Member is meaningful for Member & Anomaly; greyed for Mean & Spread.
  btn.disabled = stat === "mean" || stat === "spread";
  btn.textContent =
    stat === "mean" ? "ensemble mean (30)" :
    stat === "spread" ? "ensemble spread (30)" : member;
}

function updateControlStates() {
  memberBtnState(el("member-btn"), state.stat, state.member);
  memberBtnState(el("member-btn-b"), state.b.stat, state.b.member);
  el("compare-block").hidden = !state.compare;
  el("particles-toggle").disabled = state.compare;
  const play = el("play-btn");
  play.disabled = state.compare;
  play.title = state.compare
    ? "Playback is unavailable in compare mode"
    : "Animate 12 months from the selected date";
  // Level dropdown greys out unless a pressure-level variable is active.
  el("plev-btn").disabled = !(meta && meta.variables[state.var] && meta.variables[state.var].plev);
}

const EXP_SHORT = { historical: "Historical", "scenarioSSP5-85": "SSP5-8.5" };

// ---- floating info popover for the (i) marks
let infoPopover = null;
function hideInfoPopover() {
  if (infoPopover) { infoPopover.remove(); infoPopover = null; }
}
function showInfoPopover(text, x, y) {
  hideInfoPopover();
  infoPopover = document.createElement("div");
  infoPopover.className = "info-popover";
  const closeBtn = document.createElement("button");
  closeBtn.className = "info-popover-x";
  closeBtn.title = "Close";
  closeBtn.textContent = "✕";
  closeBtn.addEventListener("click", hideInfoPopover);
  const span = document.createElement("span");
  span.textContent = text;
  infoPopover.append(closeBtn, span);
  // clicking inside the popover keeps it open; the X (or any outside
  // click) closes it
  infoPopover.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(infoPopover);
  const r = infoPopover.getBoundingClientRect();
  infoPopover.style.left = `${Math.min(x, window.innerWidth - r.width - 10)}px`;
  infoPopover.style.top = `${y + 14}px`;
}
document.addEventListener("click", hideInfoPopover);

// Variable buttons live in three groups (surface/TOA, pressure levels,
// ocean); selecting in one group clears the active state in all.
function buildVarButtons() {
  const containers = {
    sfc: el("var-seg-sfc"),
    plev: el("var-seg-plev"),
    ocean: el("var-seg-ocean"),
  };
  Object.values(containers).forEach((c) => (c.innerHTML = ""));
  for (const [k, v] of Object.entries(meta.variables)) {
    const target = v.group === "Omon" ? containers.ocean : v.plev ? containers.plev : containers.sfc;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.value = k;
    btn.title = v.long;
    // Label with the store's actual variable name; ⓘ carries the full name.
    btn.textContent = k;
    const i = document.createElement("span");
    i.className = "info-i";
    i.textContent = "ⓘ";
    i.addEventListener("click", (e) => {
      e.stopPropagation();
      showInfoPopover(v.long, e.clientX, e.clientY);
    });
    btn.appendChild(i);
    if (k === state.var) btn.classList.add("active");
    btn.addEventListener("click", () => {
      if (state.var === k) return;
      document
        .querySelectorAll("#var-seg-sfc button, #var-seg-plev button, #var-seg-ocean button")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.var = k;
      state.filter = { lo: null, hi: null }; // filter bounds are per-variable
      rebuildUnitSeg();
      updateControlStates();
      loadField();
    });
    target.appendChild(btn);
  }
}

function buildPlevDropdown() {
  const btn = el("plev-btn");
  const list = el("plev-list");
  btn.textContent = `${state.plev} hPa`;
  list.innerHTML = "";
  for (const p of meta.levels) {
    const item = document.createElement("div");
    item.className = "dd-item" + (p === state.plev ? " selected" : "");
    item.textContent = `${p} hPa`;
    item.addEventListener("click", () => {
      state.plev = p;
      btn.textContent = `${p} hPa`;
      list.querySelectorAll(".dd-item").forEach((i) =>
        i.classList.toggle("selected", i.textContent === `${p} hPa`)
      );
      list.hidden = true;
      if (meta.variables[state.var].plev) loadField();
    });
    list.appendChild(item);
  }
  btn.addEventListener("click", (e) => {
    if (btn.disabled) return;
    e.stopPropagation();
    document.querySelectorAll(".dd-list").forEach((l) => {
      if (l !== list) l.hidden = true;
    });
    list.hidden = !list.hidden;
  });
}

async function init() {
  meta = await fetchJSON("/api/meta");

  // Restore the previous session's selections (validated against meta).
  const STAT_IDS = ["raw", "mean", "spread", "anom"];
  if (SAVED) {
    if (meta.variables[SAVED.var]) state.var = SAVED.var;
    if (meta.experiments[SAVED.experiment]) state.experiment = SAVED.experiment;
    if (meta.members.includes(SAVED.member)) state.member = SAVED.member;
    if (STAT_IDS.includes(SAVED.stat)) state.stat = SAVED.stat;
    else if (SAVED.ensmean) state.stat = "mean"; // legacy saved state
    state.compare = !!SAVED.compare;
    if (meta.levels.includes(SAVED.plev)) state.plev = SAVED.plev;
    if (SAVED.b) {
      if (meta.experiments[SAVED.b.experiment]) state.b.experiment = SAVED.b.experiment;
      if (meta.members.includes(SAVED.b.member)) state.b.member = SAVED.b.member;
      if (STAT_IDS.includes(SAVED.b.stat)) state.b.stat = SAVED.b.stat;
      else if (SAVED.b.ensmean) state.b.stat = "mean";
    }
    if (SAVED.units) Object.assign(state.units, SAVED.units);
  }

  const expInfos = Object.fromEntries(
    Object.entries(meta.experiments).map(([k, v]) => [k, v.long])
  );

  buildVarButtons();
  rebuildUnitSeg();
  buildPlevDropdown();
  buildSeg(
    "exp-seg",
    Object.keys(meta.experiments).map((k) => [k, EXP_SHORT[k] || k]),
    state.experiment,
    (v) => {
      state.experiment = v;
      // Selecting a scenario jumps to its first available month.
      el("month-input").value = meta.experiments[v].start;
      applyExperimentRange("month-input", v);
      tsRange.start = "";
      tsSetRangeDefaults();
      loadField();
    },
    expInfos
  );
  buildSeg(
    "exp-seg-b",
    Object.keys(meta.experiments).map((k) => [k, EXP_SHORT[k] || k]),
    state.b.experiment,
    (v) => {
      state.b.experiment = v;
      el("month-input-b").value = meta.experiments[v].start;
      applyExperimentRange("month-input-b", v);
      loadField();
    },
    expInfos
  );
  buildMemberDropdown("member-btn", "member-list", meta.members,
    () => state.member, (m) => (state.member = m));
  buildMemberDropdown("member-btn-b", "member-list-b", meta.members,
    () => state.b.member, (m) => (state.b.member = m));

  el("month-input").value =
    (SAVED && SAVED.timeA) || meta.experiments[state.experiment].start;
  el("month-input-b").value =
    (SAVED && SAVED.timeB) || meta.experiments[state.b.experiment].start;
  applyExperimentRange("month-input", state.experiment);
  applyExperimentRange("month-input-b", state.b.experiment);
  if (SAVED && SAVED.opacity != null) el("opacity").value = SAVED.opacity;
  if (SAVED && SAVED.particles != null) el("particles-toggle").checked = SAVED.particles;
  el("compare-toggle").checked = state.compare;

  const STAT_OPTS = [["raw", "Member"], ["mean", "Mean"], ["spread", "Spread"], ["anom", "Anomaly"]];
  const STAT_INFO = {
    raw: "Single ensemble member — one physically consistent realization of the climate (choose which member below).",
    mean: "Ensemble mean — the average of all 30 members; averages out internal variability to isolate the forced signal.",
    spread: "Ensemble spread — the standard deviation across the 30 members (sample std, N−1); maps where internal variability is largest.",
    anom: "Ensemble anomaly — the selected member minus the 30-member ensemble mean; shows that member's internal-variability excursion from the forced signal.",
  };
  buildSeg("stat-seg", STAT_OPTS, state.stat, (v) => {
    state.stat = v;
    state.filter = { lo: null, hi: null }; // scales differ between statistics
    updateControlStates();
    loadField();
  }, STAT_INFO);
  buildSeg("stat-seg-b", STAT_OPTS, state.b.stat, (v) => {
    state.b.stat = v;
    state.filter = { lo: null, hi: null };
    updateControlStates();
    loadField();
  }, STAT_INFO);

  // Month-range info marks: show the selectable date range per scenario,
  // with the side's currently selected scenario highlighted.
  const monthInfoText = (side) => {
    const cur = side === "b" ? state.b.experiment : state.experiment;
    return (
      "Available months:\n" +
      Object.entries(meta.experiments)
        .map(([k, v]) =>
          `${k === cur ? "▶ " : "   "}${EXP_SHORT[k] || k}: ${v.start} to ${v.end}`)
        .join("\n")
    );
  };
  for (const [id, side] of [["month-info-a", "a"], ["month-info-b", "b"]]) {
    el(id).addEventListener("click", (e) => {
      e.stopPropagation();
      showInfoPopover(monthInfoText(side), e.clientX, e.clientY);
    });
    el(id).addEventListener("mouseenter", () => { el(id).title = monthInfoText(side); });
  }

  el("month-input").addEventListener("change", loadField);
  el("month-input-b").addEventListener("change", loadField);
  el("prev-month").addEventListener("click", () => stepMonth("month-input", -1));
  el("next-month").addEventListener("click", () => stepMonth("month-input", 1));
  el("prev-month-b").addEventListener("click", () => stepMonth("month-input-b", -1));
  el("next-month-b").addEventListener("click", () => stepMonth("month-input-b", 1));
  el("compare-toggle").addEventListener("change", (e) => {
    state.compare = e.target.checked;
    state.filter = { lo: null, hi: null }; // diff scale differs from absolute
    updateControlStates();
    loadField();
  });
  el("opacity").addEventListener("input", () => {
    for (const o of overlays) o.setOpacity(el("opacity").value / 100);
    saveState();
  });
  const download = (format) => {
    const params = new URLSearchParams({
      format,
      var: state.var,
      experiment: state.experiment,
      member: state.member,
      stat: state.stat,
      time: el("month-input").value,
    });
    if (meta.variables[state.var].plev) params.set("plev", state.plev);
    if (state.compare) {
      params.set("compare", "true");
      params.set("experiment_b", state.b.experiment);
      params.set("member_b", state.b.member);
      params.set("stat_b", state.b.stat);
      params.set("time_b", el("month-input-b").value);
    }
    const a = document.createElement("a");
    a.href = `/api/download?${params}`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  el("dl-nc").addEventListener("click", () => download("nc"));
  el("dl-csv").addEventListener("click", () => download("csv"));
  el("particles-toggle").addEventListener("change", () => {
    if (particlesEnabled()) { resetParticles(); startParticles(); }
    else stopParticles();
    saveState();
  });

  updateControlStates();
  tsSetRangeDefaults();
  // restore the previous session's virtual stations
  if (SAVED && Array.isArray(SAVED.pins)) {
    for (const pin of SAVED.pins.slice(0, MAX_PINNED)) {
      if (Number.isFinite(pin.lat) && Number.isFinite(pin.lng)) {
        addPinnedPoint(L.latLng(pin.lat, pin.lng));
        if (pin.collapsed) pinnedPoints[pinnedPoints.length - 1].collapsed = true;
      }
    }
    refreshPins();
    restoreTsJobs(SAVED); // async; thumbnails/cards appear when validated
  }
  initFilterControls();
  initParticleCanvas();
  loadBorders();
  addGeoLabels();
  addCountryLabels();
  addStateLabels();
  await loadField();
  // NOTE: a year playback deliberately does NOT resume after a refresh —
  // restoring it meant re-preloading all 12 frames on every page load.
  // restore the globe view (orientation, zoom, spin) if that's where the user was
  if (SAVED && SAVED.globe && typeof globe !== "undefined") {
    const gs = SAVED.globe;
    if (Number.isFinite(gs.lon0)) globe.lon0 = ((gs.lon0 + 540) % 360) - 180;
    if (Number.isFinite(gs.lat0)) globe.lat0 = Math.min(85, Math.max(-85, gs.lat0));
    if (Number.isFinite(gs.zoom)) globe.zoom = Math.min(56, Math.max(0.6, gs.zoom));
    if (gs.open) {
      await toggleGlobe({ sync: false });
      if (gs.spin) setGlobeSpin(true);
    }
  }
}

init().catch((err) => setStatus(String(err.message || err), true));

// ------------------------------------------------- station time series
// EXPERIMENTAL. Time is chunked monthly in the store, so extraction reads
// one chunk per month (x30 for ensemble statistics) — the server runs it
// as a job and reports REAL progress, which we poll into the overlay.
let adhocTs = { job: null, ready: false }; // extraction at a plain-clicked point
let currentExtraction = null;              // {job, pin|null} while running
let tsDoneJobs = [];                       // completed jobs, newest first (SPEAK)
let tsPollTimer = null;
let tsCardCount = 0;
let tsCardZ = 1170;

// What a ts section refers to: station index (>=0) or the ad-hoc popup (-1).
function targetInfoByIdx(idx) {
  if (idx >= 0 && pinnedPoints[idx]) {
    const p = pinnedPoints[idx];
    return { idx, sid: idx + 1, latlng: p.latlng, job: p.tsJob, ready: !!p.tsReady, pin: p };
  }
  return { idx: -1, sid: 0, latlng: lastClickLatLng, job: adhocTs.job, ready: adhocTs.ready, pin: null };
}

// Range state lives here (not in DOM) because the popup's inputs are
// re-created every time the popup content re-renders.
const tsRange = { start: "", end: "" };

function tsSetRangeDefaults() {
  const exp = meta.experiments[state.experiment];
  if (!tsRange.start || tsRange.start < exp.start || tsRange.start > exp.end) {
    tsRange.start = exp.start;
    const [y] = exp.start.split("-").map(Number);
    const endDefault = `${Math.min(y + 9, +exp.end.slice(0, 4))}-12`;
    tsRange.end = endDefault <= exp.end ? endDefault : exp.end;
  }
}

// Latest end month allowed for a given start (hard 10-year window),
// clamped to the experiment's last month.
function tsMaxEnd(start, expEnd) {
  const limit = ((meta && meta.ts_limits && meta.ts_limits.months) || 120) - 1;
  const [y, m] = start.split("-").map(Number);
  const total = (m - 1) + limit;
  const maxEnd = `${String(y + Math.floor(total / 12)).padStart(4, "0")}-${String((total % 12) + 1).padStart(2, "0")}`;
  return maxEnd < expEnd ? maxEnd : expEnd;
}

// Inline handlers (card HTML is regenerated on every readout refresh).
// Accepts typed "YYYY-MM" or a bare "YYYY" (start -> Jan, end -> Dec).
function tsRangeChanged(input, which) {
  let v = input.value.trim();
  if (/^\d{4}$/.test(v)) v = `${v}-${which === "start" ? "01" : "12"}`;
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(v)) {
    setStatus("Time series: type dates as YYYY-MM (or just YYYY)", true);
    refreshReadout();
    return;
  }
  const exp = meta.experiments[state.experiment];
  if (v < exp.start) v = exp.start;
  if (v > exp.end) v = exp.end;
  tsRange[which] = v;
  if (tsRange.start) {
    const maxEnd = tsMaxEnd(tsRange.start, exp.end);
    if (!tsRange.end || tsRange.end > maxEnd) tsRange.end = maxEnd;
    if (tsRange.end < tsRange.start) tsRange.end = tsRange.start;
  }
  // re-render all cards so every station shows the shared range
  refreshReadout();
}

const TS_INFO_TEXT =
  "Extracts a monthly time series (up to 10 years) at this station for the " +
  "current variable, scenario, statistic and level — plotted in the " +
  "station's color (matching its map badge), with PNG/CSV/NetCDF download. " +
  "The time frame is shared by every station: set it once, then click " +
  "Extract in each station's popup for the ones you want. With two or more " +
  "extracted, 'Merge Plots' (bottom right) combines them into one figure " +
  "with a legend in the same colors. The lock icon by the coordinates lets " +
  "you type exact station coordinates (moving a station clears its " +
  "extraction). First extractions are slow — the store is chunked monthly, " +
  "so each month is a separate cloud read (×30 for ensemble statistics).";

function tsInfoClick(ev) {
  ev.stopPropagation();
  showInfoPopover(TS_INFO_TEXT, ev.clientX, ev.clientY);
}

function pinRemoveIdx(idx, ev) {
  ev.stopPropagation();
  if (pinnedPoints[idx]) removePin(pinnedPoints[idx]);
}

function tsSectionHTML(idx) {
  if (!meta) return "";
  const t = targetInfoByIdx(idx);
  const who = t.sid ? `station ${t.sid}` : "this point";
  return (
    `<div class="popup-ts">` +
    `<div class="popup-ts-title">` +
    `<span class="popup-ts-dot" style="background:${stationColor(t.sid)}"></span>` +
    `Station time series ` +
    `<span class="info-i" onclick="tsInfoClick(event)" ` +
    `title="${TS_INFO_TEXT.replace(/"/g, "&quot;")}">&#9432;</span></div>` +
    `<div class="popup-ts-range">` +
    `<input type="text" inputmode="numeric" placeholder="YYYY-MM" value="${tsRange.start}" ` +
    `onchange="tsRangeChanged(this,'start')" onclick="event.stopPropagation()" /> &rarr; ` +
    `<input type="text" inputmode="numeric" placeholder="YYYY-MM" value="${tsRange.end}" ` +
    `onchange="tsRangeChanged(this,'end')" onclick="event.stopPropagation()" /></div>` +
    `<button type="button" class="popup-ts-btn" onclick="tsExtractFor(${idx})">` +
    `Extract plot &mdash; ${who}</button>` +
    (t.ready && t.job
      ? `<img class="popup-ts-thumb" src="/api/timeseries/plot/${t.job}.png" ` +
        `onclick="tsSpawnCard('${t.job}')" title="Click to enlarge" alt="time series preview" />`
      : "") +
    (t.pin
      ? `<div class="popup-ts-remove" onclick="pinRemoveIdx(${idx}, event)">&#10005; remove this station</div>`
      : "") +
    `</div>`
  );
}

// Each completed extraction can have its own floating, draggable plot
// card — several can be on screen at once.
function tsSpawnCard(job) {
  const existing = document.getElementById(`ts-card-${job}`);
  if (existing) {
    existing.style.zIndex = ++tsCardZ;
    return;
  }
  const div = document.createElement("div");
  div.className = "ts-float";
  div.id = `ts-card-${job}`;
  const off = 40 + (tsCardCount++ % 6) * 32;
  div.style.left = `${Math.min(off + 300, window.innerWidth - 400)}px`;
  div.style.top = `${off}px`;
  div.style.zIndex = ++tsCardZ;
  div.innerHTML =
    `<div class="ts-float-head"><span>Station time series</span>` +
    `<button type="button" class="ts-float-close" title="Close">&#10005;</button></div>` +
    `<img src="/api/timeseries/plot/${job}.png" alt="time series plot" />` +
    `<div class="ts-card-actions">` +
    `<button type="button" data-fmt="png">&#8595; PNG</button>` +
    `<button type="button" data-fmt="csv">&#8595; CSV</button>` +
    `<button type="button" data-fmt="nc">&#8595; NetCDF</button></div>`;
  document.body.appendChild(div);
  div.addEventListener("mousedown", () => { div.style.zIndex = ++tsCardZ; });
  div.querySelector(".ts-float-close").addEventListener("click", () => {
    div.remove();
    saveState();
  });
  saveState();
  div.querySelectorAll(".ts-card-actions button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const fmt = btn.dataset.fmt;
      if (fmt === "png") {
        const a = document.createElement("a");
        a.href = `/api/timeseries/plot/${job}.png`;
        a.download = `spear_timeseries_${job}.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } else {
        window.open(`/api/timeseries/data/${job}?format=${fmt}`, "_blank");
      }
    });
  });
  // drag by the header
  const head = div.querySelector(".ts-float-head");
  head.addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) return;
    const r = div.getBoundingClientRect();
    const drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    const move = (ev) => {
      div.style.left = `${Math.min(Math.max(ev.clientX - drag.dx, 4), window.innerWidth - 120)}px`;
      div.style.top = `${Math.min(Math.max(ev.clientY - drag.dy, 4), window.innerHeight - 40)}px`;
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      saveState(); // remember card geometry across refreshes
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    e.preventDefault();
  });
}

// Extraction results survive refreshes: the server keeps recent jobs in
// memory; we persist the ids + card geometry and re-validate on load
// (jobs vanish gracefully if the server restarted or evicted them).
async function restoreTsJobs(saved) {
  const ids = new Set();
  ((saved.ts && saved.ts.done) || []).forEach((j) => typeof j === "string" && ids.add(j));
  (saved.pins || []).forEach((sp) => sp && typeof sp.job === "string" && ids.add(sp.job));
  if (saved.ts && typeof saved.ts.adhoc === "string") ids.add(saved.ts.adhoc);
  if (!ids.size) return;
  const valid = {};
  await Promise.all([...ids].map(async (j) => {
    try {
      const s = await fetchJSON(`/api/timeseries/status/${j}`);
      if (s.done && !s.error) valid[j] = true;
    } catch { /* job gone (server restart/eviction) — drop it */ }
  }));
  (saved.pins || []).forEach((sp, k) => {
    if (sp && valid[sp.job] && pinnedPoints[k]) {
      pinnedPoints[k].tsJob = sp.job;
      pinnedPoints[k].tsReady = true;
    }
  });
  if (saved.ts && valid[saved.ts.adhoc]) adhocTs = { job: saved.ts.adhoc, ready: true };
  tsDoneJobs = ((saved.ts && saved.ts.done) || []).filter((j) => valid[j]);
  refreshPins();
  updateMergeButton();
  for (const c of (saved.ts && saved.ts.cards) || []) {
    if (!valid[c.job]) continue;
    tsSpawnCard(c.job);
    const card = document.getElementById(`ts-card-${c.job}`);
    if (card && Number.isFinite(c.left)) {
      card.style.left = `${Math.min(Math.max(c.left, 0), window.innerWidth - 120)}px`;
      card.style.top = `${Math.min(Math.max(c.top, 0), window.innerHeight - 60)}px`;
      if (Number.isFinite(c.width)) {
        card.style.width = `${c.width}px`;
        card.style.height = `${c.height}px`;
      }
    }
  }
}

window.addEventListener("beforeunload", () => { try { saveState(); } catch {} });

// ------------------------------------------------- year playback
// Windy-style play: preload 12 monthly frames from the selected date
// (fields + wind, real progress), pre-render them to images on a COMMON
// color scale, then animate by swapping overlay URLs.
const playback = { frames: null, idx: 0, timer: null, playing: false, abort: false, loading: false };

function playbackMonths() {
  const exp = meta.experiments[state.experiment];
  const list = [];
  let [y, m] = el("month-input").value.split("-").map(Number);
  for (let i = 0; i < 12; i++) {
    const s = `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}`;
    if (s > exp.end) break;
    list.push(s);
    m += 1;
    if (m > 12) { m = 1; y += 1; }
  }
  return list;
}

function playbackCleanup() {
  if (playback.frames) {
    for (const fr of playback.frames) if (fr.url) URL.revokeObjectURL(fr.url);
  }
  playback.frames = null;
}

// Full teardown: discard the loaded year entirely (used when the user
// changes any selection, or starts a fresh year while paused).
function discardPlayback() {
  if (playback.timer) clearInterval(playback.timer);
  playback.timer = null;
  playback.playing = false;
  playback.abort = true;
  el("play-btn").textContent = "▶ Play year";
  el("play-btn").classList.remove("playing");
  el("continue-btn").hidden = true;
  const frames = playback.frames;
  playback.frames = null;
  updateTimeline(); // fall back to the always-on month navigator
  if (frames) {
    setTimeout(() => frames.forEach((fr) => fr.url && URL.revokeObjectURL(fr.url)), 2500);
  }
}

// Pause keeps the loaded year: the timeline stays up on the highlighted
// month, and Continue resumes it. "Play year" while paused starts a fresh
// year from the paused month.
function pausePlayback() {
  if (playback.timer) clearInterval(playback.timer);
  playback.timer = null;
  playback.playing = false;
  el("play-btn").textContent = "▶ Play year";
  el("play-btn").classList.remove("playing");
  el("continue-btn").hidden = false;
  const fr = playback.frames[playback.idx];
  setStatus(
    `⏸ ${statPrefix(fr.field)}${fr.field.label} · ${selLabel(fr.field)} ` +
    `(paused ${playback.idx + 1}/${playback.frames.length})`
  );
  saveState(); // remember the paused sim across refreshes
}

function continuePlayback() {
  if (!playback.frames) return;
  playback.playing = true;
  el("play-btn").textContent = "⏸ Pause";
  el("play-btn").classList.add("playing");
  el("continue-btn").hidden = true;
  stepPlayback();
  playback.timer = setInterval(stepPlayback, 750);
}

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Idle mode: the bar is a permanent month navigator anchored on the
// selected date — the FIRST cell is the panel's selected month (which on
// a fresh session is the scenario's first month), showing the 12-month
// window a "Play year" would animate. Clicking a month selects it and
// re-anchors the bar there. During playback the bar shows the loaded
// frame window instead (see buildPlayTimeline).
function buildIdleTimeline() {
  const bar = el("play-timeline");
  bar.innerHTML = "";
  const current = el("month-input").value;
  for (const s of playbackMonths()) {
    const [y, m] = s.split("-").map(Number);
    const cell = document.createElement("span");
    cell.innerHTML = `${MONTH_ABBR[m - 1]}<small>${y}</small>`;
    cell.title = s;
    cell.addEventListener("click", () => {
      el("month-input").value = s;
      loadField();
    });
    if (s === current) cell.classList.add("active");
    bar.appendChild(cell);
  }
  bar.hidden = false;
}

function updateTimeline() {
  if (!meta) return;
  if (playback.frames) return; // playback owns the bar while a year is loaded
  buildIdleTimeline();
}

function buildPlayTimeline() {
  const bar = el("play-timeline");
  bar.innerHTML = "";
  playback.frames.forEach((fr, i) => {
    const [y, m] = fr.field.time.split("-");
    const cell = document.createElement("span");
    cell.innerHTML = `${MONTH_ABBR[+m - 1]}<small>${y}</small>`;
    cell.title = fr.field.time;
    cell.addEventListener("click", () => {
      playback.idx = i - 1;
      stepPlayback();
      if (playback.playing && playback.timer) {
        // restart the tick phase so the clicked month gets a full dwell
        clearInterval(playback.timer);
        playback.timer = setInterval(stepPlayback, 750);
      }
    });
    bar.appendChild(cell);
  });
  bar.hidden = false;
}

function stepPlayback() {
  if (!playback.frames || !playback.frames.length) return;
  playback.idx = (playback.idx + 1) % playback.frames.length;
  const fr = playback.frames[playback.idx];
  applyOverlayUrl(fr.url);
  currentField = fr.field;
  currentDisplay = fr.field;
  if (fr.wind) currentWind = fr.wind;
  el("month-input").value = fr.field.time;
  [...el("play-timeline").children].forEach((c, i) =>
    c.classList.toggle("active", i === playback.idx)
  );
  if (typeof globe !== "undefined" && globe.open) globeRender(false);
  setStatus(
    `▶ ${statPrefix(fr.field)}${fr.field.label} · ${selLabel(fr.field)} ` +
    `(${playback.idx + 1}/${playback.frames.length})`
  );
  refreshReadout();
}

async function startPlayback(resume) {
  const resuming = resume && Number.isFinite(resume.idx);
  if (playback.playing) {
    pausePlayback();
    return;
  }
  if (playback.loading) return;
  if (playback.frames) {
    // paused: "Play year" starts a NEW year from the paused month
    // (month-input already sits on the paused frame)
    discardPlayback();
  }
  if (state.compare) return;
  const months = playbackMonths();
  if (months.length < 2) {
    setStatus("Play: fewer than 2 months remain after this date", true);
    return;
  }
  playback.abort = false;
  playback.loading = true;
  const isPlev = meta.variables[state.var].plev;
  el("load-overlay").hidden = false;
  el("load-title").textContent = "Preloading year…";
  el("load-pct").textContent = "0%";
  el("load-note").innerHTML =
    `Fetching ${months.length} monthly frames` +
    (state.stat !== "raw" ? " (ensemble statistics read all 30 members per month)" : "") +
    (isPlev ? " on the selected pressure level" : "") +
    ".<br>Cached months are instant; progress is real.";
  el("load-cancel").hidden = false;
  const withWind = el("particles-toggle").checked;
  const total = months.length * (withWind ? 2 : 1);
  let done = 0;
  const bump = () => { el("load-pct").textContent = `${Math.round((++done / total) * 90)}%`; };
  try {
    const fields = [], winds = [];
    for (const mth of months) {
      if (playback.abort) throw new Error("cancelled");
      const f = await fetchField({
        experiment: state.experiment, member: state.member, stat: state.stat, time: mth,
      });
      f._grid = toFloat32(f.values, f.nlat, f.nlon);
      f.values = null; // free ~1MB of parsed JSON per frame
      fields.push(f);
      bump();
      if (withWind) {
        if (playback.abort) throw new Error("cancelled");
        const wp = {
          experiment: state.experiment,
          member: state.stat === "raw" ? state.member : "ensmean",
          time: mth,
        };
        if (isPlev) wp.plev = state.plev;
        const w = await fetchJSON(`/api/wind?${new URLSearchParams(wp)}`);
        w._u = toFloat32(w.u, w.nlat, w.nlon);
        w._v = toFloat32(w.v, w.nlat, w.nlon);
        w.u = w.v = null;
        winds.push(w);
        bump();
      }
    }
    // One shared color scale for the whole year so the legend/colors don't
    // flicker as auto-scaled fields rescale per month.
    const cfg = VAR_CONFIG[state.var];
    let lut;
    const statDyn = fields[0].stat === "anom" ? "div" : fields[0].stat === "spread" ? "seq" : null;
    const dynMode = statDyn || cfg.dynamic || null;
    if (dynMode === "div") {
      let L = 0;
      for (const f of fields) {
        const [lo, hi] = samplePercentiles(f._grid, 0.02, 0.98);
        L = Math.max(L, Math.abs(lo), Math.abs(hi));
      }
      L = niceLimit(L);
      lut = makeLUT(-L, L, (v, out) => diffColor(v, L, out));
      for (const f of fields) f._dyn = { min: -L, max: L, limit: L, div: true };
    } else if (dynMode === "seq") {
      let lo = Infinity, hi = -Infinity;
      for (const f of fields) {
        const [a, b2] = samplePercentiles(f._grid, 0.02, 0.98);
        lo = Math.min(lo, a);
        hi = Math.max(hi, b2);
      }
      if (!(hi > lo)) hi = lo + 1;
      const ramp = fields[0].stat === "spread" ? "plasma" : cfg.ramp;
      lut = makeLUT(lo, hi, (v, out) => rampColor(ramp, (v - lo) / (hi - lo), 0.88, out));
      for (const f of fields) f._dyn = { min: lo, max: hi, ramp };
    } else {
      lut = buildLUT(state.var);
    }
    currentLUT = lut; // globe + re-renders use the common year scale
    el("load-title").textContent = "Rendering frames…";
    playback.frames = [];
    for (let i = 0; i < fields.length; i++) {
      if (playback.abort) throw new Error("cancelled");
      const url = await renderFieldToURL(fields[i], lut, state.filter);
      playback.frames.push({ field: fields[i], wind: winds[i] || null, url });
      el("load-pct").textContent = `${90 + Math.round(((i + 1) / fields.length) * 10)}%`;
    }
    el("load-overlay").hidden = true;
    el("load-cancel").hidden = true;
    playback.loading = false;
    playback.playing = true;
    playback.idx = -1;
    el("play-btn").textContent = "⏸ Pause";
    el("play-btn").classList.add("playing");
    buildPlayTimeline();
    const n = playback.frames.length;
    playback.idx = resuming ? ((resume.idx % n) + n) % n - 1 : -1;
    stepPlayback();
    renderMeta(); // legend once, on the common scale
    if (resuming && !resume.playing) {
      pausePlayback();
    } else {
      playback.timer = setInterval(stepPlayback, 750);
    }
  } catch (e) {
    el("load-overlay").hidden = true;
    el("load-cancel").hidden = true;
    playback.loading = false;
    playbackCleanup();
    if (String(e.message || e) === "cancelled") setStatus("Playback preload cancelled");
    else setStatus(`Play: ${e.message || e}`, true);
  }
}

el("play-btn").addEventListener("click", startPlayback);
el("continue-btn").addEventListener("click", continuePlayback);

// ------------------------------------------------- experimental 3-D globe
// Orthographic projection rendered per-pixel from the SAME field grid,
// LUT and filter the flat map uses — no 3-D library. Drag rotates,
// scroll zooms; the globe re-renders on any selection change and even
// animates during year playback.
const globe = {
  open: false, lon0: 0, lat0: 20, zoom: 1,
  coast: null, borders: null, raf: null,
};
const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

async function globeLoadGeo() {
  if (globe.coast) return;
  [globe.coast, globe.borders, globe.states, globe.countryLabels, globe.stateLabels] =
    await Promise.all([
      fetch("geo/ne_50m_coastline.json").then((r) => r.json()),
      fetch("geo/ne_50m_admin_0_boundary_lines_land.json").then((r) => r.json()),
      fetch("geo/ne_50m_admin_1_states_provinces_lines.json").then((r) => r.json()),
      fetch("geo/country_labels.json").then((r) => r.json()),
      fetch("geo/us_state_labels.json").then((r) => r.json()),
    ]);
}

function globeRender(fast) {
  if (!globe.open || !currentDisplay || !currentLUT) return;
  const canvas = el("globe-canvas");
  // fast may be a number (explicit render scale) or a boolean (drag preview)
  const scale = typeof fast === "number" ? fast
    : fast ? 0.5 : Math.min(window.devicePixelRatio || 1, 1.5);
  const W = Math.max(2, Math.floor(window.innerWidth * scale));
  const H = Math.max(2, Math.floor(window.innerHeight * scale));
  canvas.width = W;
  canvas.height = H;
  canvas.style.width = `${window.innerWidth}px`;
  canvas.style.height = `${window.innerHeight}px`;
  const ctx = canvas.getContext("2d");
  const R = Math.min(W, H) * 0.42 * globe.zoom;
  const cx = W / 2, cy = H / 2;
  const phi0 = globe.lat0 * D2R, lam0 = globe.lon0 * D2R;
  const sp0 = Math.sin(phi0), cp0 = Math.cos(phi0);
  const d = currentDisplay, grid = d._grid, lut = currentLUT;
  const fLo = state.filter.lo, fHi = state.filter.hi;
  const img = ctx.createImageData(W, H);
  const px = img.data;
  const y0 = Math.max(0, Math.floor(cy - R)), y1 = Math.min(H, Math.ceil(cy + R));
  const x0 = Math.max(0, Math.floor(cx - R)), x1 = Math.min(W, Math.ceil(cx + R));
  for (let iy = y0; iy < y1; iy++) {
    const y = (cy - iy) / R;
    for (let ix = x0; ix < x1; ix++) {
      const x = (ix - cx) / R;
      const rho2 = x * x + y * y;
      if (rho2 > 1) continue;
      const cosc = Math.sqrt(1 - rho2);
      const lat = Math.asin(cosc * sp0 + y * cp0) * R2D;
      const lon = (lam0 + Math.atan2(x, cosc * cp0 - y * sp0)) * R2D;
      // base sphere tone with limb shading; field color composited on top
      const shade = 0.7 + 0.3 * cosc;
      let r = 38, g = 41, b2 = 50;
      const v = sampleGrid(d, grid, lat, lon);
      if (v != null && !(fLo != null && v < fLo) && !(fHi != null && v > fHi)) {
        let idx = ((v - lut.min) * lut.scale) | 0;
        if (idx < 0) idx = 0;
        else if (idx > lut.last) idx = lut.last;
        const li = idx * 4;
        const a = lut.data[li + 3] / 255;
        r = lut.data[li] * a + r * (1 - a);
        g = lut.data[li + 1] * a + g * (1 - a);
        b2 = lut.data[li + 2] * a + b2 * (1 - a);
      }
      const k = (iy * W + ix) * 4;
      px[k] = r * shade;
      px[k + 1] = g * shade;
      px[k + 2] = b2 * shade;
      px[k + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);

  // vector overlays: project, drop back-hemisphere segments
  const proj = (lon, lat) => {
    const phi = lat * D2R, dl = lon * D2R - lam0;
    const cosc = sp0 * Math.sin(phi) + cp0 * Math.cos(phi) * Math.cos(dl);
    if (cosc < 0.001) return null;
    return [
      cx + R * Math.cos(phi) * Math.sin(dl),
      cy - R * (cp0 * Math.sin(phi) - sp0 * Math.cos(phi) * Math.cos(dl)),
    ];
  };
  const drawLines = (geo, style, width) => {
    if (!geo) return;
    ctx.strokeStyle = style;
    ctx.lineWidth = width * scale;
    ctx.beginPath();
    for (const ft of geo.features) {
      const geom = ft.geometry;
      const lines = geom.type === "LineString" ? [geom.coordinates] : geom.coordinates;
      for (const line of lines) {
        let pen = false;
        for (const c of line) {
          const p = proj(c[0], c[1]);
          if (!p) { pen = false; continue; }
          if (pen) ctx.lineTo(p[0], p[1]);
          else ctx.moveTo(p[0], p[1]);
          pen = true;
        }
      }
    }
    ctx.stroke();
  };
  // same colors/weights/opacities as the flat map's border layers
  drawLines(globe.coast, "rgba(232,230,223,0.66)", 1.2);
  drawLines(globe.borders, "rgba(217,215,207,0.62)", 1.0);
  if (globe.zoom >= 2.0) drawLines(globe.states, "rgba(201,199,191,0.55)", 0.9);
  // graticule every 30°
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = scale;
  ctx.beginPath();
  const gstep = Math.max(0.2, 2 / globe.zoom); // finer sampling when zoomed in
  const gspace = globe.zoom >= 6 ? 5 : globe.zoom >= 2.5 ? 10 : 30;
  if (gratState.lon) {
    for (let lon = -180; lon < 180; lon += gspace) {
      let pen = false;
      for (let lat = -90; lat <= 90; lat += gstep) {
        const p = proj(lon, lat);
        if (!p) { pen = false; continue; }
        pen ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]);
        pen = true;
      }
    }
  }
  if (gratState.lat) {
    const glat = gspace === 30 ? 60 : 80;
    for (let lat = -glat; lat <= glat; lat += gspace) {
      let pen = false;
      for (let lon = -180; lon <= 180; lon += gstep) {
        const p = proj(lon, lat);
        if (!p) { pen = false; continue; }
        pen ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]);
        pen = true;
      }
    }
  }
  ctx.stroke();

  // ---- place labels, tiered by globe zoom like the flat map's zoom levels.
  // Labels fade out toward the limb and never overlap (first-come priority:
  // continents > oceans > major countries > other countries > US states).
  const projLabel = (lon, lat) => {
    const phi = lat * D2R, dl = lon * D2R - lam0;
    const cosc = sp0 * Math.sin(phi) + cp0 * Math.cos(phi) * Math.cos(dl);
    return [
      cx + R * Math.cos(phi) * Math.sin(dl),
      cy - R * (cp0 * Math.sin(phi) - sp0 * Math.cos(phi) * Math.cos(dl)),
      cosc,
    ];
  };
  const placed = [];
  const drawLabel = (txt, lon, lat, sizePx, fill, opts = {}) => {
    const p = projLabel(lon, lat);
    if (p[2] < 0.22) return; // limb / backside
    ctx.font = `${opts.bold ? "600 " : ""}${opts.italic ? "italic " : ""}${Math.round(sizePx * scale)}px system-ui, sans-serif`;
    const tw = ctx.measureText(txt).width;
    const th = sizePx * scale;
    for (const r of placed) {
      if (Math.abs(p[0] - r.x) < (tw + r.w) / 2 + 6 * scale &&
          Math.abs(p[1] - r.y) < (th + r.h) / 2 + 4 * scale) return;
    }
    placed.push({ x: p[0], y: p[1], w: tw, h: th });
    ctx.globalAlpha = Math.min(1, (p[2] - 0.22) / 0.2) * (opts.alpha || 1);
    ctx.strokeStyle = "rgba(8,10,15,0.9)";
    ctx.lineWidth = 3.5 * scale;
    ctx.strokeText(txt, p[0], p[1]);
    ctx.fillStyle = fill;
    ctx.fillText(txt, p[0], p[1]);
    ctx.globalAlpha = 1;
  };
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  const z = globe.zoom;
  if (z <= 1.7) {
    for (const g of GEO_LABELS) {
      if (g.ocean) drawLabel(g.name, g.lon, g.lat, 14, "rgba(170,198,228,0.95)", { italic: true });
      else drawLabel(g.name.toUpperCase(), g.lon, g.lat, 16, "rgba(246,244,238,0.95)", { bold: true });
    }
  }
  if (globe.countryLabels && z >= 1.2) {
    for (const c of globe.countryLabels) {
      if (c.r <= 3 || z >= 1.9) drawLabel(c.n, c.lon, c.lat, 13, "rgba(240,238,232,0.92)");
    }
  }
  if (globe.stateLabels && z >= 2.4) {
    for (const s of globe.stateLabels) {
      drawLabel(s.n, s.lon, s.lat, 12, "rgba(230,228,222,0.9)");
    }
  }
  // graticule degree labels along the view-center lines (drawn last so
  // place-name labels win any collision)
  if (gratState.lat || gratState.lon) {
    const gsp = globe.zoom >= 6 ? 5 : globe.zoom >= 2.5 ? 10 : 30;
    if (gratState.lat) {
      const gl = gsp === 30 ? 60 : 80;
      for (let la = -gl; la <= gl; la += gsp) {
        drawLabel(fmtLat(la), globe.lon0, la, 14, "rgba(215,225,236,0.95)", { bold: true });
      }
    }
    if (gratState.lon) {
      for (let lo = -180; lo < 180; lo += gsp) {
        drawLabel(fmtLon(lo), lo, globe.lat0, 14, "rgba(215,225,236,0.95)", { bold: true });
      }
    }
  }

  // rim
  ctx.strokeStyle = "rgba(255,255,255,0.22)";
  ctx.lineWidth = 1.5 * scale;
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.stroke();

  globeSyncPinPositions(); // stations/popup ride the sphere as it moves
}

// forward/inverse orthographic projection in CSS pixels (for DOM overlays
// and click handling; globeRender uses its own scaled version internally)
function globeProjectCSS(lat, lng) {
  const g = globeGeom();
  const phi0 = globe.lat0 * D2R, lam0 = globe.lon0 * D2R;
  const sp0 = Math.sin(phi0), cp0 = Math.cos(phi0);
  const phi = lat * D2R, dl = lng * D2R - lam0;
  const cosc = sp0 * Math.sin(phi) + cp0 * Math.cos(phi) * Math.cos(dl);
  return {
    x: g.cx + g.R * Math.cos(phi) * Math.sin(dl),
    y: g.cy - g.R * (cp0 * Math.sin(phi) - sp0 * Math.cos(phi) * Math.cos(dl)),
    cosc,
  };
}

function globeUnproject(cssX, cssY) {
  const g = globeGeom();
  const x = (cssX - g.cx) / g.R, y = (g.cy - cssY) / g.R;
  const rho2 = x * x + y * y;
  if (rho2 > 1) return null; // off the sphere
  const cosc = Math.sqrt(1 - rho2);
  const phi0 = globe.lat0 * D2R, lam0 = globe.lon0 * D2R;
  const lat = Math.asin(cosc * Math.sin(phi0) + y * Math.cos(phi0)) * R2D;
  const lng = (lam0 + Math.atan2(x, cosc * Math.cos(phi0) - y * Math.sin(phi0))) * R2D;
  return L.latLng(lat, ((lng + 540) % 360) - 180);
}

// ---- stations + click popup on the globe: the SAME card HTML the flat
// map uses (readoutContent/pinCompactValue), rendered into an overlay div
// and re-projected onto the sphere every frame.
function globeSyncPinContent() {
  const host = el("globe-pins");
  host.innerHTML = "";
  globe.pinEls = [];
  globe.adhocEl = null;
  if (!currentDisplay) return;
  pinnedPoints.forEach((p, i) => {
    const badge = document.createElement("span");
    badge.className = "pin-badge globe-pin-badge";
    badge.style.background = stationColor(i + 1);
    badge.textContent = String(i + 1);
    badge.title = "Ctrl+click to remove this station";
    badge.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (ev.ctrlKey || ev.metaKey) removePin(p);
    });
    const card = document.createElement("div");
    card.className = "globe-pin-card pin-tip";
    const head =
      `<span class="pin-num">#${i + 1}</span>` +
      `<span class="pin-collapse" onclick="pinToggleCollapse(${i}, event)" ` +
      `title="${p.collapsed ? "Expand" : "Collapse"}">${p.collapsed ? "&#9656;" : "&#9662;"}</span>`;
    card.innerHTML = p.collapsed
      ? `<div>${head} ${pinCompactValue(p.latlng)}</div>`
      : `<div>${head}</div>${readoutContent(p.latlng, true, i)}`;
    host.appendChild(card);
    host.appendChild(badge);
    globe.pinEls.push({ badge, card, p });
  });
  if (globe.adhocOpen && lastClickLatLng) {
    const card = document.createElement("div");
    card.className = "globe-pin-card pin-tip globe-adhoc";
    card.innerHTML =
      `<span class="globe-card-x" title="Close">&#10005;</span>` +
      readoutContent(lastClickLatLng, true, -1);
    card.querySelector(".globe-card-x").addEventListener("click", () => {
      globe.adhocOpen = false;
      globeSyncPinContent();
    });
    host.appendChild(card);
    globe.adhocEl = card;
  }
  globeSyncPinPositions();
}

function globeSyncPinPositions() {
  if (!globe.open || !globe.pinEls) return;
  const place = (node, ll, lift) => {
    const pr = globeProjectCSS(ll.lat, ll.lng);
    const vis = pr.cosc > 0.15; // hide as the point rotates past the limb
    node.style.display = vis ? "" : "none";
    if (vis) {
      node.style.left = `${pr.x}px`;
      node.style.top = `${pr.y - lift}px`;
    }
  };
  for (const e of globe.pinEls) {
    place(e.badge, e.p.latlng, 0);
    place(e.card, e.p.latlng, 16);
  }
  if (globe.adhocEl && lastClickLatLng) place(globe.adhocEl, lastClickLatLng, 10);
}

function globeHandleClick(ll, ctrl) {
  if (!currentDisplay) return;
  // Plain clicks do NOTHING on the globe: grabbing to rotate often ends
  // with the pointer barely moving, which would register as a click and
  // pop a card. Everything is Ctrl+click here — stations carry the full
  // readout, so no separate click-to-inspect popup is needed.
  if (!ctrl) return;
  // Ctrl+clicking near an existing pin (globe screen space) removes it
  for (const p of pinnedPoints) {
    const pr = globeProjectCSS(p.latlng.lat, p.latlng.lng);
    const at = globeProjectCSS(ll.lat, ll.lng);
    if (pr.cosc > 0 && Math.hypot(pr.x - at.x, pr.y - at.y) < 16) {
      removePin(p);
      return;
    }
  }
  createPin(ll);
}

function globeScheduleFull() {
  clearTimeout(globe.fullTimer);
  // while auto-rotating, the spin loop owns rendering — skip the full pass
  globe.fullTimer = setTimeout(() => { if (!globe.spin) globeRender(false); }, 160);
}

// map zoom <-> globe zoom conversion: match degrees-per-pixel at center
function globePxPerDeg() {
  return (Math.min(window.innerWidth, window.innerHeight) * 0.42 * Math.PI) / 180;
}
function globeZoomFromMap(z) {
  return Math.min(56, Math.max(0.6, (256 * Math.pow(2, z)) / 360 / globePxPerDeg()));
}
function mapZoomFromGlobe(g) {
  return Math.round(Math.log2((g * globePxPerDeg() * 360) / 256));
}

async function toggleGlobe(opts = {}) {
  // The two views stay "related": opening the globe starts it at the map's
  // center/zoom, and returning to the map recenters on where the globe was
  // left. Saved-state restore passes {sync:false} to keep its own view.
  const sync = opts.sync !== false;
  globe.open = !globe.open;
  if (globe.open && sync) {
    const c = map.getCenter();
    globe.lat0 = Math.min(85, Math.max(-85, c.lat));
    globe.lon0 = ((c.lng + 540) % 360 + 360) % 360 - 180;
    globe.zoom = globeZoomFromMap(map.getZoom());
  } else if (!globe.open && sync) {
    map.setView(
      [globe.lat0, ((globe.lon0 + 540) % 360 + 360) % 360 - 180],
      Math.min(9, Math.max(2, mapZoomFromGlobe(globe.zoom)))
    );
  }
  el("globe-view").hidden = !globe.open;
  el("globe-btn").textContent = globe.open ? "Map" : "Globe";
  el("globe-btn").title = globe.open
    ? "Return to the flat map view"
    : "Experimental: view the field on a rotatable 3-D globe";
  el("globe-btn").classList.toggle("active", globe.open);
  el("rotate-btn").hidden = !globe.open;
  el("globe-note").hidden = !globe.open;
  if (globe.open) {
    await globeLoadGeo();
    globeSyncPinContent();
    globeRender(false);
    startGlobeParticles();
  } else {
    setGlobeSpin(false);
    stopGlobeParticles();
  }
  saveState();
}

// ---- slow auto-rotation: the Earth turns eastward under a fixed viewer;
// wind particles and labels keep animating on the moving sphere.
const GLOBE_SPIN_RATE = 0.06; // degrees of longitude per 60fps frame (~3.6°/s)

function globeSpinStep(now) {
  globe.spinRaf = requestAnimationFrame(globeSpinStep);
  const dtf = Math.min(3, Math.max(0.2, (now - globe.spinLast) / 16.7));
  globe.spinLast = now;
  if (globe.spinPaused || !globe.open) return;
  // Earth spins eastward: to a fixed viewer, surface features drift
  // west→east (left to right), so the camera's center longitude DECREASES.
  globe.lon0 -= GLOBE_SPIN_RATE * dtf;
  if (globe.lon0 < -180) globe.lon0 += 360;
  // Render at ~30fps (rotation still advances every frame via dtf): a
  // 60fps repaint starved the particle loop and stretched its time steps,
  // which visibly lengthened the trails whenever the globe was spinning.
  if (now - (globe.spinRenderT || 0) >= 33) {
    globe.spinRenderT = now;
    globeRender(0.7); // mid resolution: smooth spin, sharper than drag preview
  }
}

function setGlobeSpin(on) {
  globe.spin = on;
  el("rotate-btn").textContent = on ? "Stop Rotation" : "Start Rotation";
  el("rotate-btn").classList.toggle("active", on);
  if (on && globe.spinRaf == null) {
    globe.spinLast = performance.now();
    globe.spinPaused = false;
    globe.spinRaf = requestAnimationFrame(globeSpinStep);
  } else if (!on && globe.spinRaf != null) {
    cancelAnimationFrame(globe.spinRaf);
    globe.spinRaf = null;
    if (globe.open) globeRender(false); // settle at full resolution
  }
  saveState();
}

el("rotate-btn").addEventListener("click", () => setGlobeSpin(!globe.spin));

el("globe-btn").addEventListener("click", toggleGlobe);
window.addEventListener("resize", () => {
  if (!globe.open) return;
  globeScheduleFull();
  sizeGlobeParticleCanvas();
  resetGlobeParticles();
});

// drag to rotate, scroll to zoom
{
  const canvas = el("globe-canvas");
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, moved: false };
    canvas.classList.add("dragging");
    canvas.setPointerCapture(e.pointerId);
    globe.spinPaused = true; // hand-dragging takes over while pressed
    gparticles.paused = true;
    clearGlobeParticleCanvas();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 5) drag.moved = true;
    if (!drag.moved) return; // still a potential click — don't rotate yet
    const degPerPx = 0.25 / globe.zoom;
    globe.lon0 -= (e.clientX - drag.x) * degPerPx;
    globe.lat0 += (e.clientY - drag.y) * degPerPx;
    globe.lat0 = Math.min(85, Math.max(-85, globe.lat0));
    globe.lon0 = ((globe.lon0 + 540) % 360) - 180;
    drag.x = e.clientX; // keep sx/sy/moved intact — recreating the object
    drag.y = e.clientY; // here silently killed the drag after one step

    if (!globe.raf) {
      globe.raf = requestAnimationFrame(() => {
        globe.raf = null;
        globeRender(true);
      });
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    const wasClick = drag && !drag.moved;
    drag = null;
    canvas.classList.remove("dragging");
    globeScheduleFull();
    resetGlobeParticles();
    gparticles.paused = false;
    globe.spinPaused = false; // resume auto-rotation if it was on
    if (wasClick) {
      const ll = globeUnproject(e.clientX, e.clientY);
      if (ll) globeHandleClick(ll, e.ctrlKey || e.metaKey);
    }
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    // 56x ~= the flat map's max Leaflet zoom (9) in degrees-per-pixel
    globe.zoom = Math.min(56, Math.max(0.6, globe.zoom * (e.deltaY < 0 ? 1.15 : 0.87)));
    globeRender(true);
    globeScheduleFull();
    resetGlobeParticles();
  }, { passive: false });
}

// ---- wind particles on the globe: same wind grid and trail styling as the
// flat map, but advected in lat/lon and forward-projected orthographically
// each frame. Particles respawn when they age out or rotate past the limb.
const gparticles = { canvas: null, ctx: null, list: [], raf: null, lastT: 0, paused: false };

function globeGeom() {
  const w = window.innerWidth, h = window.innerHeight;
  return { w, h, cx: w / 2, cy: h / 2, R: Math.min(w, h) * 0.42 * globe.zoom };
}

function sizeGlobeParticleCanvas() {
  const canvas = el("globe-particles");
  const dpr = window.devicePixelRatio || 1;
  const w = window.innerWidth, h = window.innerHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
  }
  gparticles.canvas = canvas;
  gparticles.ctx = canvas.getContext("2d");
  gparticles.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function clearGlobeParticleCanvas() {
  if (gparticles.ctx) {
    gparticles.ctx.save();
    gparticles.ctx.setTransform(1, 0, 0, 1, 0, 0);
    gparticles.ctx.clearRect(0, 0, gparticles.canvas.width, gparticles.canvas.height);
    gparticles.ctx.restore();
  }
}

// Uniform over the VISIBLE part of the sphere: sample screen pixels and
// keep ones that land on the disk (zoomed in, the disk is mostly
// offscreen — sampling it directly would strand nearly all particles
// outside the viewport).
function spawnGlobeParticle(p, g) {
  const phi0 = globe.lat0 * D2R, lam0 = globe.lon0 * D2R;
  const sp0 = Math.sin(phi0), cp0 = Math.cos(phi0);
  let x = 0, y = 0, ok = false;
  for (let i = 0; i < 12 && !ok; i++) {
    x = (Math.random() * g.w - g.cx) / g.R;
    y = (g.cy - Math.random() * g.h) / g.R;
    ok = x * x + y * y < 0.998;
  }
  if (!ok) { // screen misses the disk (extreme pan) — fall back to the disk
    const t = 2 * Math.PI * Math.random();
    const rr = Math.sqrt(Math.random()) * 0.999;
    x = rr * Math.cos(t);
    y = rr * Math.sin(t);
  }
  const cosc = Math.sqrt(Math.max(0, 1 - x * x - y * y));
  p.lat = Math.asin(cosc * sp0 + y * cp0) * R2D;
  p.lon = (lam0 + Math.atan2(x, cosc * cp0 - y * sp0)) * R2D;
  p.hist = [p.lat, p.lon]; // trail history in lat/lon (oldest first)
  p.age = Math.floor(Math.random() * PARTICLE_MAX_AGE);
  return p;
}

function resetGlobeParticles() {
  clearGlobeParticleCanvas();
  const g = globeGeom();
  const visible = Math.min(Math.PI * g.R * g.R, g.w * g.h); // disk ∩ screen
  const n = Math.max(300, Math.min(3500, Math.round(visible / 500)));
  gparticles.list = Array.from({ length: n }, () => spawnGlobeParticle({}, g));
}

// Trail history length in points (~half a second of motion at 30fps).
const GP_HIST = 14;

function globeStepParticles(now) {
  gparticles.raf = requestAnimationFrame(globeStepParticles);
  if (now - gparticles.lastT < 30) return; // same ~30fps cap as the flat map
  const dtf = Math.min(3, Math.max(0.5, (now - gparticles.lastT) / 16.7));
  gparticles.lastT = now;
  if (!globe.open || gparticles.paused) return;
  if (!particlesEnabled()) { clearGlobeParticleCanvas(); return; }

  const w = currentWind;
  const g = globeGeom();
  const ctx = gparticles.ctx;
  // Slow down as the globe zooms in, mirroring the flat map's behaviour;
  // intensity differences between regions are preserved (no normalisation).
  const speed = PARTICLE_SPEED * Math.min(1.4, Math.max(0.05, 1 / globe.zoom));
  const phi0 = globe.lat0 * D2R, lam0 = globe.lon0 * D2R;
  const sp0 = Math.sin(phi0), cp0 = Math.cos(phi0);

  // Advection is wind-only; the globe's rotation never enters particle
  // state, so spinning cannot change how fast trails appear to move.
  for (const p of gparticles.list) {
    p.age += dtf;
    const u = sampleGrid(w, w._u, p.lat, p.lon);
    const v = sampleGrid(w, w._v, p.lat, p.lon);
    if (u == null || v == null || p.age > PARTICLE_MAX_AGE) {
      spawnGlobeParticle(p, g);
      continue;
    }
    p.lon += (u * speed * dtf) / Math.max(Math.cos((p.lat * Math.PI) / 180), 0.05);
    p.lat += v * speed * dtf;
    if (p.lat > 89 || p.lat < -89) { spawnGlobeParticle(p, g); continue; }
    p.hist.push(p.lat, p.lon);
    if (p.hist.length > GP_HIST * 2) p.hist.splice(0, p.hist.length - GP_HIST * 2);
  }

  // Redraw every trail from its lat/lon history, projected with the
  // CURRENT rotation. Trails are rigidly attached to the sphere: a
  // rotating globe shows the identical animation, just turned. (The flat
  // map's cheaper screen-space fade can't do this — old trail pixels
  // would stay put while the sphere moves beneath them.)
  clearGlobeParticleCanvas();
  ctx.lineWidth = 1.1;
  // three alpha bands approximate the fading-comet look: newest brightest
  const bands = [[0.18, 13, 8], [0.42, 8, 4], [0.65, 4, 0]]; // [alpha, from-seg-back, to-seg-back]
  for (const [alpha, back0, back1] of bands) {
    ctx.strokeStyle = `rgba(235, 240, 245, ${alpha})`;
    ctx.beginPath();
    for (const p of gparticles.list) {
      const n = p.hist.length / 2;
      if (n < 2) continue;
      const k0 = Math.max(0, n - 1 - back0), k1 = Math.max(0, n - 1 - back1);
      if (k1 <= k0) continue;
      let pen = false;
      for (let k = k0; k <= k1; k++) {
        const phi = p.hist[k * 2] * D2R, dl = p.hist[k * 2 + 1] * D2R - lam0;
        const cosc = sp0 * Math.sin(phi) + cp0 * Math.cos(phi) * Math.cos(dl);
        if (cosc < 0.02) { pen = false; continue; } // limb-clip this piece
        const x = g.cx + g.R * Math.cos(phi) * Math.sin(dl);
        const y = g.cy - g.R * (cp0 * Math.sin(phi) - sp0 * Math.cos(phi) * Math.cos(dl));
        pen ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        pen = true;
      }
    }
    ctx.stroke();
  }

  // respawn particles that are no longer visible: rotated to the far
  // side, or (zoomed in) drifted outside the viewport
  for (const p of gparticles.list) {
    const phi = p.lat * D2R, dl = p.lon * D2R - lam0;
    const cosc = sp0 * Math.sin(phi) + cp0 * Math.cos(phi) * Math.cos(dl);
    if (cosc < 0.02) { spawnGlobeParticle(p, g); continue; }
    const x = g.cx + g.R * Math.cos(phi) * Math.sin(dl);
    const y = g.cy - g.R * (cp0 * Math.sin(phi) - sp0 * Math.cos(phi) * Math.cos(dl));
    if (x < -40 || x > g.w + 40 || y < -40 || y > g.h + 40) spawnGlobeParticle(p, g);
  }
}

function startGlobeParticles() {
  sizeGlobeParticleCanvas();
  resetGlobeParticles();
  gparticles.paused = false;
  if (gparticles.raf == null) {
    gparticles.lastT = performance.now();
    gparticles.raf = requestAnimationFrame(globeStepParticles);
  }
}

function stopGlobeParticles() {
  if (gparticles.raf != null) {
    cancelAnimationFrame(gparticles.raf);
    gparticles.raf = null;
  }
  clearGlobeParticleCanvas();
}

function updateMergeButton() {
  const ready = pinnedPoints.filter((p) => p.tsReady && p.tsJob);
  const btn = el("merge-plots");
  btn.hidden = ready.length < 2;
  if (!btn.hidden) btn.textContent = `Merge Plots (${ready.length})`;
}

async function mergePlots() {
  const jobs = pinnedPoints.filter((p) => p.tsReady && p.tsJob).map((p) => p.tsJob);
  if (jobs.length < 2) return;
  try {
    const r = await fetchJSON2("/api/timeseries/merge", { jobs });
    tsDoneJobs.unshift(r.job);
    tsDoneJobs = tsDoneJobs.slice(0, 8);
    tsSpawnCard(r.job);
  } catch (e) {
    setStatus(`Merge: ${e.message || e}`, true);
  }
}

function tsShowProgress() {
  el("load-overlay").hidden = false;
  el("load-title").textContent = "Extracting time series…";
  el("load-pct").textContent = "0%";
  el("load-note").innerHTML =
    "Reading one data chunk per month from the cloud store" +
    (state.stat !== "raw" ? " (×30 members for ensemble statistics)" : "") +
    ".<br>Progress shown is real, not estimated.";
  el("load-cancel").hidden = false;
}

function tsHideProgress() {
  el("load-overlay").hidden = true;
  el("load-cancel").hidden = true;
  if (tsPollTimer) { clearTimeout(tsPollTimer); tsPollTimer = null; }
}

async function tsCancel() {
  const job = currentExtraction ? currentExtraction.job : null;
  currentExtraction = null;
  tsHideProgress();
  setStatus("Time series extraction cancelled");
  if (job) {
    try {
      await fetch(`/api/timeseries/cancel/${job}`, { method: "POST" });
    } catch { /* server may already be done; nothing to do */ }
  }
}

el("load-cancel").addEventListener("click", () => {
  if (typeof playback !== "undefined" && playback.loading) {
    playback.abort = true; // the preload loop notices and aborts
  } else {
    tsCancel();
  }
});
el("merge-plots").addEventListener("click", mergePlots);

function tsMonthCount(start, end) {
  const [y0, m0] = start.split("-").map(Number);
  const [y1, m1] = end.split("-").map(Number);
  return (y1 - y0) * 12 + (m1 - m0) + 1;
}

async function tsPoll(job) {
  try {
    const s = await fetchJSON(`/api/timeseries/status/${job}`);
    if (!currentExtraction || currentExtraction.job !== job) return; // superseded/cancelled
    if (s.error) {
      tsHideProgress();
      setStatus(`Time series failed: ${s.error}`, true);
      return;
    }
    el("load-pct").textContent = `${Math.round(s.progress * 100)}%`;
    if (s.done) {
      tsHideProgress();
      const ctx = currentExtraction && currentExtraction.job === job ? currentExtraction : null;
      if (ctx && ctx.pin) {
        ctx.pin.tsJob = job;
        ctx.pin.tsReady = true;
      } else if (ctx) {
        adhocTs = { job, ready: true };
      }
      currentExtraction = null;
      tsDoneJobs.unshift(job);
      tsDoneJobs = tsDoneJobs.slice(0, 8);
      updateMergeButton();
      // thumbnails render in the station cards / popup; click to enlarge
      refreshReadout();
      return;
    }
  } catch (e) {
    tsHideProgress();
    setStatus(`Time series failed: ${e.message || e}`, true);
    return;
  }
  tsPollTimer = setTimeout(() => tsPoll(job), 800);
}

async function tsExtractFor(idx) {
  const t = targetInfoByIdx(idx);
  if (!t || !t.latlng) return;
  const points = [{ lat: t.latlng.lat, lon: t.latlng.lng }];
  const start = tsRange.start;
  const end = tsRange.end;
  if (!start || !end || end < start) {
    setStatus("Time series: pick a valid start/end month", true);
    return;
  }
  const n = tsMonthCount(start, end);
  const limit = (meta && meta.ts_limits && meta.ts_limits.months) || 120;
  if (n > limit) {
    setStatus(
      `Time series: max ${Math.floor(limit / 12)} years (${limit} months) per extraction; you asked for ${n}`,
      true
    );
    return;
  }
  const payload = {
    var: state.var,
    experiment: state.experiment,
    member: state.member,
    stat: state.stat,
    points,
    station_ids: [t.sid],
    start,
    end,
  };
  if (meta.variables[state.var].plev) payload.plev = state.plev;
  try {
    const r = await fetchJSON2("/api/timeseries/start", payload);
    currentExtraction = { job: r.job, pin: t.pin || null };
    tsShowProgress();
    tsPoll(r.job);
  } catch (e) {
    setStatus(`Time series: ${e.message || e}`, true);
  }
}

async function fetchJSON2(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}


// -------------------------------------------------------- Ask SPEAR chat
const chat = { messages: [], busy: false };

// Convert any LaTeX the model slips through into readable plain text.
function stripLatex(t) {
  return t.replace(/\$([^$\n]+)\$/g, (_, x) =>
    x
      .replace(/\\text\{([^}]*)\}/g, "$1")
      .replace(/\^\\circ|\\degree/g, "°")
      .replace(/\\%/g, "%")
      .replace(/\\times/g, "×")
      .replace(/\\,|\\;|\\!/g, " ")
      .replace(/\\[a-zA-Z]+/g, "")
      .replace(/[{}]/g, "")
  );
}

// Minimal safe markdown: escape everything, then re-allow bold and code.
function renderChatText(div, text) {
  const escaped = stripLatex(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  div.innerHTML = escaped
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^#{1,4}\s*(.+)$/gm, "<strong>$1</strong>")
    .replace(/^\* /gm, "• ");
}

function chatEl(cls, text) {
  const div = document.createElement("div");
  div.className = `chat-msg ${cls}`;
  if (cls.startsWith("bot") && !cls.includes("typing") && !cls.includes("error")) {
    renderChatText(div, text);
  } else {
    div.textContent = text;
  }
  el("chat-messages").appendChild(div);
  el("chat-messages").scrollTop = el("chat-messages").scrollHeight;
  return div;
}

function viewSnapshot() {
  const snap = {
    var: state.var,
    experiment: state.experiment,
    member: state.member,
    stat: state.stat,
    time: el("month-input").value,
    compare: state.compare,
  };
  if (meta && meta.variables[state.var] && meta.variables[state.var].plev) {
    snap.plev = state.plev;
  }
  if (state.compare) {
    snap.experiment_b = state.b.experiment;
    snap.member_b = state.b.member;
    snap.stat_b = state.b.stat;
    snap.time_b = el("month-input-b").value;
  }
  if (lastClickLatLng) {
    snap.click = { lat: lastClickLatLng.lat, lon: lastClickLatLng.lng };
  }
  if (pinnedPoints.length) {
    snap.points = pinnedPoints.map((p) => ({ lat: p.latlng.lat, lon: p.latlng.lng }));
  }
  // lets SPEAK see the most recent extractions (newest first, up to 3)
  if (typeof tsDoneJobs !== "undefined" && tsDoneJobs.length) {
    snap.ts_jobs = tsDoneJobs.slice(0, 3);
  }
  return snap;
}

async function sendChat(text) {
  if (chat.busy) return;
  chat.busy = true;
  el("chat-send").disabled = true;
  chat.messages.push({ role: "user", content: text });
  chatEl("user", text);
  const typing = chatEl("bot typing", "");
  typing.innerHTML =
    '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>';
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chat.messages.slice(-12), view: viewSnapshot() }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    typing.remove();
    chat.messages.push({ role: "assistant", content: data.reply });
    chatEl("bot", data.reply);
  } catch (err) {
    typing.remove();
    chatEl("bot error", `Sorry — ${err.message || err}`);
    chat.messages.pop(); // let the user retry the same question
  } finally {
    chat.busy = false;
    el("chat-send").disabled = false;
  }
}

// info mark on the bubble and window title
const ASK_INFO_TEXT =
  "SPEAK — the SPEAR Knowledge Assistant. It can answer questions about the " +
  "data on screen — it sees your current variable, scenario, member, month and " +
  "level, live statistics of the displayed field (and both sides of a " +
  "comparison), plus the value at your last clicked point — and it draws on " +
  "SPEAR model documentation for background.";
for (const id of ["ask-info", "ask-info-2"]) {
  el(id).addEventListener("click", (e) => {
    e.stopPropagation();
    showInfoPopover(ASK_INFO_TEXT, e.clientX, e.clientY);
  });
}
el("ask-info").title = ASK_INFO_TEXT;
el("ask-info-2").title = ASK_INFO_TEXT;

// open / close / minimize
el("ask-bubble").addEventListener("click", () => {
  el("chat-panel").hidden = false;
  el("ask-bubble").hidden = true;
  el("chat-text").focus();
});
el("chat-close").addEventListener("click", () => {
  el("chat-panel").hidden = true;
  el("ask-bubble").hidden = false;
});
el("chat-min").addEventListener("click", () => {
  el("chat-panel").classList.toggle("minimized");
});
el("chat-max").addEventListener("click", () => {
  const panel = el("chat-panel");
  panel.classList.remove("minimized");
  panel.classList.toggle("maximized");
  // Clear any manual corner-resize so the maximized size wins cleanly.
  if (panel.classList.contains("maximized")) {
    panel.style.width = "";
    panel.style.height = "";
  }
});

// submit (Enter sends, Shift+Enter for a newline)
el("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = el("chat-text").value.trim();
  if (!text) return;
  el("chat-text").value = "";
  sendChat(text);
});
el("chat-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    el("chat-form").requestSubmit();
  }
});

// drag the window by its header (buttons excluded)
(() => {
  const panel = el("chat-panel");
  const header = el("chat-header");
  let drag = null;
  header.addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) return;
    const r = panel.getBoundingClientRect();
    // switch from right-anchored to explicit left/top once dragging starts
    panel.style.left = `${r.left}px`;
    panel.style.top = `${r.top}px`;
    panel.style.right = "auto";
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const w = panel.offsetWidth, h = panel.offsetHeight;
    const x = Math.min(Math.max(e.clientX - drag.dx, 4), window.innerWidth - Math.min(w, 160));
    const y = Math.min(Math.max(e.clientY - drag.dy, 4), window.innerHeight - 40);
    panel.style.left = `${x}px`;
    panel.style.top = `${y}px`;
  });
  document.addEventListener("mouseup", () => { drag = null; });
})();
