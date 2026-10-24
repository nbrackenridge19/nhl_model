/* SAA / SAR engine (client side). Mirrors saa_engine.py, which is validated against the workbook to ~1e-14.
   Pure functions, no DOM -- shared by the page and by the node parity test. */
const SK_CATS_F = ['GP','G','A','P2GA','PIM','SOG','STP','Hit','Blk','TK','Cor'];
const SK_CATS_D = ['GP','PTD','G','A','P2GA','PIM','SOG','STP','Hit','Blk','TK','Cor'];
const SK_PG = ['PTD','G','A','P2GA','PIM','SOG','STP','Hit','Blk','TK','Cor'];
const G_CATS = ['GP','W','GAA','SV','SHO'];
const G_MIN_GP = 5;

function xlRound(x, nd = 2) {               // Excel ROUND: half away from zero
  const m = Math.pow(10, nd);
  return Math.sign(x) * Math.floor(Math.abs(x) * m + 0.5 + 1e-9) / m;
}
function median(a) {
  if (!a.length) return NaN;
  const s = a.slice().sort((x, y) => x - y), n = s.length;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}
function sd(a) {                              // sample SD (Excel STDEV)
  const n = a.length; if (n < 2) return NaN;
  const m = a.reduce((x, y) => x + y, 0) / n;
  return Math.sqrt(a.reduce((x, y) => x + (y - m) * (y - m), 0) / (n - 1));
}
function percentileInc(a, p) {                // Excel PERCENTILE.INC
  if (!a.length) return 0;
  const s = a.slice().sort((x, y) => x - y), k = (s.length - 1) * p, f = Math.floor(k), c = Math.min(f + 1, s.length - 1);
  return s[f] + (s[c] - s[f]) * (k - f);
}

/* Window totals for one player. seasons = list of season keys to pool (e.g. ['2526','2627']).
   Skater season record: {gp,g,a,pim,sog,stp,hit,blk,tk,cf,ca}. Pooled by summing totals (== games-weighted).
   A stat that is missing (null) in a season is pooled only over seasons where it exists. */
function skaterWindow(p, seasons) {
  const t = { GP: 0, G: 0, A: 0, PIM: 0, SOG: 0, STP: 0, Hit: 0, Blk: 0, TK: 0, Cor: 0 };
  const gpFor = { TK: 0, Cor: 0 };           // games where the stat exists
  let any = false;
  for (const s of seasons) {
    const r = p.s && p.s[s]; if (!r || !r.gp) continue;
    any = true; t.GP += r.gp; t.G += r.g || 0; t.A += r.a || 0; t.PIM += r.pim || 0; t.SOG += r.sog || 0;
    t.STP += r.stp || 0; t.Hit += r.hit || 0; t.Blk += r.blk || 0;
    if (r.tk != null) { t.TK += r.tk; gpFor.TK += r.gp; }
    if (r.cf != null && r.ca != null) { t.Cor += r.cf - r.ca; gpFor.Cor += r.gp; }
  }
  if (!any) return null;
  t.gpFor = gpFor; return t;
}

function perGame(t, grp) {
  const pg = {};
  const q = (x, g) => (g > 0 ? xlRound(x / g) : NaN);
  pg.G = q(t.G, t.GP); pg.A = q(t.A, t.GP);
  pg.PTD = grp === 'D' ? q(t.G + t.A, t.GP) : NaN;
  pg.P2GA = q(2 * t.G + t.A, t.GP);
  pg.PIM = q(t.PIM, t.GP); pg.SOG = q(t.SOG, t.GP); pg.STP = q(t.STP, t.GP);
  pg.Hit = q(t.Hit, t.GP); pg.Blk = q(t.Blk, t.GP);
  pg.TK = t.gpFor.TK ? xlRound(t.TK / t.gpFor.TK) : NaN;
  pg.Cor = t.gpFor.Cor ? xlRound(t.Cor / t.gpFor.Cor) : NaN;
  return pg;
}
const catVal = (r, c) => (c === 'GP' ? r.GP : r.pg[c]);
const finite = (a) => a.filter((v) => Number.isFinite(v));

/* rows: [{grp:'F'|'D', GP, pg:{...}, pool:bool, fa:bool}]  ->  params per group */
function skaterParams(rows, useGP = true) {
  const P = {};
  for (const grp of ['F', 'D']) {
    const g = rows.filter((r) => r.grp === grp);
    const ros = g.filter((r) => r.pool), fa = g.filter((r) => r.fa);
    const fgp = fa.filter((r) => r.GP > 0).map((r) => r.GP);
    const thr = fgp.length ? percentileInc(fgp, 0.25) : 0;
    const fq = fa.filter((r) => r.GP > thr);
    const p = { threshold: thr, nPool: ros.length, nRepl: fq.length };
    for (const c of (grp === 'D' ? SK_CATS_D : SK_CATS_F)) {
      const vr = finite(ros.map((r) => catVal(r, c))), vf = finite(fq.map((r) => catVal(r, c)));
      p[c] = { med: median(vr), sd: sd(vr), repl: median(vf) };
    }
    P[grp] = p;
  }
  return P;
}
/* adds r.saa{cat}, r.sar{cat}, r.tSAA, r.tSAR (NaN when GP < floor) */
function scoreSkaters(rows, P, floor = 0, useGP = true) {
  for (const r of rows) {
    const cats = (r.grp === 'D' ? SK_CATS_D : SK_CATS_F).filter((c) => useGP || c !== 'GP');
    r.saa = {}; r.sar = {}; let ta = 0, tr = 0;
    for (const c of (r.grp === 'D' ? SK_CATS_D : SK_CATS_F)) {
      const q = P[r.grp][c], v = catVal(r, c);
      const a = (v - q.med) / q.sd, s = (v - q.repl) / q.sd;
      r.saa[c] = Number.isFinite(a) ? a : null; r.sar[c] = Number.isFinite(s) ? s : null;
      if (cats.includes(c)) { ta += Number.isFinite(a) ? a : 0; tr += Number.isFinite(s) ? s : 0; }
    }
    const low = r.GP < floor;
    r.tSAA = low ? NaN : ta; r.tSAR = low ? NaN : tr;
  }
  return rows;
}

/* goalies: window totals, pooled GAA = 60*GA/MIN (falls back to GP-weighted GAA), SV% = 1 - GA/SA */
function goalieWindow(p, seasons) {
  let gp = 0, w = 0, so = 0, ga = 0, sa = 0, mn = 0, gaaW = 0, gaaG = 0, svW = 0, svG = 0, any = false;
  for (const s of seasons) {
    const r = p.s && p.s[s]; if (!r || !r.gp) continue;
    any = true; gp += r.gp; w += r.w || 0; so += r.so || 0;
    if (r.ga != null && r.sa) { ga += r.ga; sa += r.sa; }
    if (r.min) mn += r.min;
    if (r.gaa != null) { gaaW += r.gaa * r.gp; gaaG += r.gp; }
    if (r.sv != null) { svW += r.sv * r.gp; svG += r.gp; }
  }
  if (!any) return null;
  const gaa = mn > 0 && ga ? (60 * ga) / mn : (gaaG ? gaaW / gaaG : NaN);
  const sv = sa > 0 ? (sa - ga) / sa : (svG ? svW / svG : NaN);
  return { GP: gp, W: w, GAA: gaa, SV: sv, SHO: so };
}
function goalieParams(rows) {
  const ros = rows.filter((r) => r.pool), fa = rows.filter((r) => r.fa);
  const fgp = fa.filter((r) => r.GP > 0).map((r) => r.GP);
  const thr = fgp.length ? percentileInc(fgp, 0.25) : 0;
  const fq = fa.filter((r) => r.GP > thr);
  const p = { threshold: thr, nPool: ros.length, nRepl: fq.length };
  for (const c of G_CATS) {
    const vr = finite(ros.map((r) => r[c])), vf = finite(fq.map((r) => r[c]));
    p[c] = { med: median(vr), sd: sd(vr), repl: median(vf) };
  }
  return p;
}
function scoreGoalies(rows, P, floor = G_MIN_GP) {
  for (const r of rows) {
    r.saa = {}; r.sar = {}; let ta = 0, tr = 0;
    for (const c of G_CATS) {
      const q = P[c], sg = c === 'GAA' ? -1 : 1;
      const a = (sg * (r[c] - q.med)) / q.sd, s = (sg * (r[c] - q.repl)) / q.sd;
      r.saa[c] = Number.isFinite(a) ? a : null; r.sar[c] = Number.isFinite(s) ? s : null;
      ta += Number.isFinite(a) ? a : 0; tr += Number.isFinite(s) ? s : 0;
    }
    const low = r.GP < Math.max(floor, G_MIN_GP);
    r.tSAA = low ? NaN : ta; r.tSAR = low ? NaN : tr;
  }
  return rows;
}
if (typeof module !== 'undefined') module.exports = { skaterWindow, perGame, skaterParams, scoreSkaters, goalieWindow, goalieParams, scoreGoalies, xlRound, median, sd, percentileInc, SK_CATS_F, SK_CATS_D, G_CATS };
