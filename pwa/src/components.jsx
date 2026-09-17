/* Shared components — trading-terminal visual language.
   Colour only ever carries meaning: green money, red risk, amber guardian,
   cyan machine. Everything else is surface/ink/hairline. */
import React, { useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { Lock, Target, TrendingUp, AlertCircle } from 'lucide-react';

/* ------------------------------------------------ live-value interactions */

/* Eases a number toward its new value instead of snapping. Respects
   prefers-reduced-motion by jumping straight to the target. */
export function useCountUp(target, duration = 500) {
  const [display, setDisplay] = useState(target ?? 0);
  const fromRef = useRef(target ?? 0);
  const rafRef = useRef(null);

  useEffect(() => {
    const to = Number(target) || 0;
    const from = Number(fromRef.current) || 0;
    if (from === to) return undefined;

    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      fromRef.current = to;
      setDisplay(to);
      return undefined;
    }

    const start = performance.now();
    const tick = (now) => {
      const p = Math.min(1, (now - start) / duration);
      const eased = 1 - (1 - p) ** 3; // easeOutCubic
      const v = from + (to - from) * eased;
      setDisplay(v);
      if (p < 1) rafRef.current = requestAnimationFrame(tick);
      else fromRef.current = to;
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [target, duration]);

  return display;
}

/* Returns a flash class whenever `value` changes — green on a tick up, red
   on a tick down. The whole point of a live terminal is that you notice
   the change without staring at it. */
export function useFlash(value) {
  const prev = useRef(value);
  const [cls, setCls] = useState('');

  useEffect(() => {
    const before = prev.current;
    prev.current = value;
    if (before === undefined || before === null || value === before) return undefined;
    if (typeof value !== 'number' || typeof before !== 'number') return undefined;
    setCls(value > before ? 'flash-up' : 'flash-down');
    const t = setTimeout(() => setCls(''), 620);
    return () => clearTimeout(t);
  }, [value]);

  return cls;
}

/* Shimmer placeholder — replaces bare "Loading…" text. */
export function Skeleton({ className = '', h = 16, w = '100%' }) {
  return <div className={`skeleton ${className}`} style={{ height: h, width: w }} />;
}

export function SkeletonCard({ rows = 3 }) {
  return (
    <Card className="space-y-3">
      <Skeleton h={10} w="35%" />
      <Skeleton h={28} w="55%" />
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} h={12} w={`${90 - i * 12}%`} />
      ))}
    </Card>
  );
}

export const rupee = (n, sign = false) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const v = Math.round(Math.abs(n)).toLocaleString('en-IN');
  const s = n < 0 ? '−' : sign ? '+' : '';
  return `${s}₹${v}`;
};

export const num = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n) ? '—' : Number(n).toFixed(d);

export const pnlColor = (n) => (n > 0 ? 'text-green' : n < 0 ? 'text-red' : 'text-ink');

export function Eyebrow({ children, className = '' }) {
  return <div className={`eyebrow ${className}`}>{children}</div>;
}

/* Surface primitive. `tone` adds a meaning-carrying ring without shouting;
   `wash` adds a soft coloured gradient behind the content; `hover` opts into
   cursor-responsive depth. */
/* Two block behaviours that belong to every card, bundled so <Card> stays a
   one-liner:
     · a spotlight — the cursor position is written to --mx/--my for
       .card::after, and only while the pointer is actually over this block,
       so it costs nothing on the other thirty;
     · a reveal — the block rises in the first time it enters the viewport.
   The reveal has a hard fallback timer because inactive tabs are display:none,
   and anything the observer somehow never sees must still end up visible. */
function useBlockFx() {
  const ref = useRef(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setShown(true);
      return undefined;
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        setShown(true);
        io.disconnect();
      },
      { threshold: 0.04, rootMargin: '0px 0px -6% 0px' },
    );
    io.observe(el);
    const failsafe = setTimeout(() => setShown(true), 1500);
    return () => {
      io.disconnect();
      clearTimeout(failsafe);
    };
  }, []);

  const onPointerMove = (e) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty('--mx', `${e.clientX - r.left}px`);
    el.style.setProperty('--my', `${e.clientY - r.top}px`);
  };

  return { ref, onPointerMove, revealCls: shown ? 'reveal reveal-in' : 'reveal' };
}

export function Card({ children, className = '', accent = false, tone = '', wash = '', hover = false }) {
  const ring = {
    ai: 'border-ai/40 shadow-glow-ai',
    green: 'border-green/40 shadow-glow-green',
    red: 'border-red/40 shadow-glow-red',
    amber: 'border-amber/40',
    violet: 'border-violet/40',
    cyan: 'border-cyan/40',
  }[tone] || '';
  const fx = useBlockFx();
  return (
    <div
      ref={fx.ref}
      onPointerMove={fx.onPointerMove}
      className={`card ${fx.revealCls} ${ring} ${wash ? `wash-${wash}` : ''} ${
        hover ? 'hoverable' : ''
      } ${accent ? 'border-ai/60 shadow-glow-ai' : ''} ${className}`}
    >
      {children}
    </div>
  );
}

/* A rupee figure that eases to its new value and flashes on change. */
export function AnimatedRupee({ value, sign = true, className = '' }) {
  const animated = useCountUp(value ?? 0);
  const flash = useFlash(value ?? 0);
  return (
    <span className={`num rounded px-1 -mx-1 ${flash} ${className}`}>
      {rupee(animated, sign)}
    </span>
  );
}

/* Section header with an optional right-hand slot — replaces the ad-hoc
   <h3> + flex rows that were scattered across every screen. */
export function SectionHeader({ title, sub, right, icon: Icon }) {
  return (
    <div className="flex items-end justify-between gap-3 mb-2.5 px-0.5">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          {Icon && <Icon size={14} className="text-muted shrink-0" strokeWidth={2.2} />}
          <h3 className="section-title truncate">{title}</h3>
        </div>
        {sub && <div className="num text-eyebrow text-muted mt-1">{sub}</div>}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}

/* The FSM made visible. Always current, top-right of every screen. */
export function StateChip({ state }) {
  const map = {
    LOCKED: ['!bg-red-soft !text-red !border-red/40', Lock, 'LOCKED'],
    EARNED: ['!bg-ai-soft !text-ai !border-ai/40', Target, 'EARNED'],
    PROTECT: ['!bg-amber-soft !text-amber !border-amber/40', AlertCircle, 'PROTECT'],
    TRAIL: ['!bg-green-soft !text-green !border-green/40', TrendingUp, 'TRAIL'],
  };
  const hit = map[state];
  if (!hit) return <span className="chip text-ink-2">NORMAL</span>;
  const [cls, Icon, label] = hit;
  return (
    <span className={`chip inline-flex items-center gap-1 font-semibold ${cls}`}>
      <Icon size={11} strokeWidth={2.5} />
      {label}
    </span>
  );
}

export function OriginTag({ origin, confidence }) {
  if (origin === 'manual') return <span className="chip !bg-amber-soft !text-amber !border-amber/40">MANUAL</span>;
  return (
    <span className="chip !bg-ai-soft !text-ai !border-ai/40">AI{confidence ? ` ${confidence}` : ''}</span>
  );
}

/* Signed delta with an arrow — the single most repeated microcopy in the app. */
export function Delta({ value, suffix = '', digits = 2 }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <span className="text-muted">—</span>;
  const up = value > 0;
  const flat = value === 0;
  return (
    <span className={`num ${flat ? 'text-ink-2' : up ? 'text-green' : 'text-red'}`}>
      {flat ? '' : up ? '▲' : '▼'} {Number(Math.abs(value)).toFixed(digits)}{suffix}
    </span>
  );
}

/* Compact labelled metric for grids. `accent` paints a left edge in a
   categorical colour so a row of tiles reads as distinct at a glance. */
export function StatTile({ label, value, sub, tone = '', accent = '', icon: Icon, className = '' }) {
  const a = {
    green: { edge: 'border-l-green', glow: 'rgba(0,217,139,0.16)', ink: 'text-green' },
    red: { edge: 'border-l-red', glow: 'rgba(255,77,94,0.16)', ink: 'text-red' },
    ai: { edge: 'border-l-ai', glow: 'rgba(77,141,255,0.16)', ink: 'text-ai' },
    violet: { edge: 'border-l-violet', glow: 'rgba(167,139,250,0.16)', ink: 'text-violet' },
    cyan: { edge: 'border-l-cyan', glow: 'rgba(34,211,238,0.16)', ink: 'text-cyan' },
    amber: { edge: 'border-l-amber', glow: 'rgba(255,176,32,0.16)', ink: 'text-amber' },
    teal: { edge: 'border-l-teal', glow: 'rgba(45,212,191,0.16)', ink: 'text-teal' },
    orange: { edge: 'border-l-orange', glow: 'rgba(251,146,60,0.16)', ink: 'text-orange' },
    pink: { edge: 'border-l-pink', glow: 'rgba(244,114,182,0.16)', ink: 'text-pink' },
  }[accent];
  return (
    <div
      className={`well relative overflow-hidden px-3 py-2.5 transition-transform duration-200 hover:-translate-y-[1px] ${
        a ? `border-l-2 ${a.edge}` : ''
      } ${className}`}
      /* The accent bleeds into the tile instead of only edging it — enough to
         tell tiles apart at a glance without another border. */
      style={a ? { backgroundImage: `radial-gradient(90% 120% at 0% 100%, ${a.glow}, transparent 70%)` } : undefined}
    >
      <div className="flex items-center gap-1.5">
        {Icon && <Icon size={11} className={`shrink-0 ${a ? a.ink : 'text-muted'}`} strokeWidth={2.2} />}
        <Eyebrow>{label}</Eyebrow>
      </div>
      <div className={`num text-contract mt-1 font-medium ${tone || 'text-ink'}`}>{value}</div>
      {sub && <div className="num text-eyebrow text-muted mt-1">{sub}</div>}
    </div>
  );
}

/* Day Rail — the signature component. Hatched red zone left of the lock,
   ticks for floor / target / trail-on, and a live dot that rides the P&L.
   The floor tick MOVES as the floor ratchets. */
export function DayRail({ dayPnl, floor, target, lossLimit }) {
  const lo = -Math.abs(lossLimit) * 1.15;
  const hi = Math.max(target * 1.45, dayPnl * 1.1, target * 1.45);
  const pct = (v) => Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));

  const lockPct = pct(-Math.abs(lossLimit));
  const floorPct = pct(floor);
  const targetPct = pct(target);
  const trailPct = pct(target * 1.3);
  const dotPct = pct(dayPnl);

  return (
    <div className="mt-4">
      <div className="relative h-2 rounded-pill bg-well border border-line/70 overflow-visible">
        <div
          className="absolute inset-y-0 left-0 rounded-l-pill"
          style={{
            width: `${lockPct}%`,
            backgroundImage:
              'repeating-linear-gradient(45deg,rgba(255,77,94,0.14) 0 4px,rgba(255,77,94,0.26) 4px 8px)',
          }}
        />
        <div className="absolute -top-1.5 w-[2px] h-5 rounded-sm bg-ink" style={{ left: `${floorPct}%` }} />
        <div className="absolute -top-1 w-[2px] h-4 rounded-sm bg-muted" style={{ left: `${targetPct}%` }} />
        <div className="absolute -top-1 w-[2px] h-4 rounded-sm bg-muted" style={{ left: `${trailPct}%` }} />
        <motion.div
          className="absolute rounded-full border-2 border-paper"
          layout
          transition={{ type: 'spring', stiffness: 100, damping: 20 }}
          style={{
            left: `calc(${dotPct}% - 8px)`,
            top: '-4px',
            width: 16,
            height: 16,
            background: dayPnl >= 0 ? '#00D98B' : '#FF4D5E',
            boxShadow: dayPnl >= 0 ? '0 0 12px rgba(0,217,139,0.5)' : '0 0 12px rgba(255,77,94,0.5)',
          }}
        />
      </div>
      <div className="num text-eyebrow text-muted flex justify-between mt-3">
        <span>lock {rupee(-Math.abs(lossLimit))}</span>
        <span className="text-ink">floor {rupee(floor)}</span>
        <span>target {rupee(target)}</span>
        <span>trail {rupee(target * 1.3)}</span>
      </div>
    </div>
  );
}

/* Distance-to-SL bar. Red→green gradient with a mark at the stop, a mark at
   the target, and the live premium riding between them. */
export function SlTrack({ sl, target, ltp, entry }) {
  const stop = Number(sl) || 0;
  const tgt = Number(target) || 0;
  const px = Number(ltp) || Number(entry) || 0;
  if (!stop || !px) return null;
  const hi = tgt > stop ? tgt : Math.max(px, entry || px) * 1.1;
  const span = hi - stop || 1;
  const clamp = (v) => Math.max(3, Math.min(97, v));
  const pct = (v) => clamp(((v - stop) / span) * 100);

  return (
    <div className="mt-4">
      <div
        className="relative h-1.5 rounded-pill"
        style={{
          background:
            'linear-gradient(90deg, rgba(255,77,94,0.35), rgba(255,255,255,0.06) 45%, rgba(0,217,139,0.35))',
        }}
      >
        <span className="absolute -top-[5px] w-0.5 h-4 rounded-sm bg-red" style={{ left: '0%' }} />
        {tgt > stop && <span className="absolute -top-[5px] w-0.5 h-4 rounded-sm bg-green" style={{ left: '100%' }} />}
        <motion.span
          className="absolute rounded-full bg-paper border-[3px] border-ink"
          layout
          transition={{ type: 'spring', stiffness: 120, damping: 20 }}
          style={{ left: `calc(${pct(px)}% - 7px)`, top: '-4px', width: 14, height: 14 }}
        />
      </div>
      <div className="num text-eyebrow text-muted flex justify-between mt-2">
        <span>SL {num(stop, 1)}</span>
        {tgt > stop && <span>target {num(tgt, 1)}</span>}
      </div>
    </div>
  );
}

/* filled = used · outline = remaining · dashed = the earned 4th */
export function TradeDots({ taken, cap, base = 3 }) {
  const dots = [];
  for (let i = 0; i < Math.max(cap, base); i += 1) {
    const used = i < taken;
    const earned = i >= base;
    dots.push(
      <span
        key={i}
        className="inline-block rounded-full mr-1.5"
        style={{
          width: 8,
          height: 8,
          background: used ? '#F4F4F6' : 'transparent',
          border: used ? 'none' : earned && cap <= base ? '1px dashed #63636E' : '1px solid #32323A',
        }}
      />,
    );
  }
  return <div className="flex items-center">{dots}</div>;
}

export function Stat({ label, value, sub, tone = '' }) {
  return (
    <div>
      <Eyebrow>{label}</Eyebrow>
      <div className={`num text-contract mt-1 font-medium ${tone}`}>{value}</div>
      {sub && <div className="num text-eyebrow text-muted mt-1">{sub}</div>}
    </div>
  );
}

export function Row({ left, right, className = '' }) {
  return (
    <div className={`flex items-center justify-between gap-3 ${className}`}>
      <div className="min-w-0">{left}</div>
      <div className="text-right shrink-0 whitespace-nowrap">{right}</div>
    </div>
  );
}

export function Empty({ children }) {
  return (
    <div className="text-center text-muted text-sec py-8 px-4 well border-dashed">
      {children}
    </div>
  );
}

export function Banner({ tone = 'amber', children }) {
  const map = {
    amber: 'bg-amber-soft text-amber border-amber/25',
    red: 'bg-red-soft text-red border-red/25',
    ink: 'bg-card-2 text-ink-2 border-line',
    ai: 'bg-ai-soft text-ai border-ai/25',
    green: 'bg-green-soft text-green border-green/25',
  };
  return (
    <div className={`num text-chip text-center py-2 px-3 rounded-block border ${map[tone] || map.amber}`}>
      {children}
    </div>
  );
}

/* Determinate progress bar for a running job. */
export function ProgressRail({ pct = 0, tone = 'ai' }) {
  const clamped = Math.max(0, Math.min(100, pct));
  const fill = { ai: 'bg-ai', green: 'bg-green', amber: 'bg-amber' }[tone] || 'bg-ai';
  return (
    <div className="relative h-2 rounded-pill bg-well border border-line/70 overflow-hidden">
      <motion.div
        className={`absolute inset-y-0 left-0 rounded-pill ${fill}`}
        animate={{ width: `${clamped}%` }}
        transition={{ type: 'spring', stiffness: 80, damping: 20 }}
      />
    </div>
  );
}

/* Tiny inline trend line — for stat tiles and table rows. */
export function Sparkline({ values = [], width = 72, height = 22, tone }) {
  if (!values || values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (i) => (i / (values.length - 1)) * width;
  const y = (v) => height - ((v - min) / span) * (height - 2) - 1;
  const d = values.map((v, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
  const rising = values[values.length - 1] >= values[0];
  const stroke = tone || (rising ? '#00D98B' : '#FF4D5E');
  return (
    <svg width={width} height={height} className="shrink-0 overflow-visible">
      <path d={`${d} L ${width} ${height} L 0 ${height} Z`} fill={stroke} opacity="0.1" />
      <path d={d} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/* Small vertical bar chart — value per label, coloured by sign. */
export function MiniBars({ items, height = 56 }) {
  if (!items || items.length === 0) return null;
  const max = Math.max(1, ...items.map((i) => Math.abs(i.value)));
  return (
    <div className="flex items-end gap-[3px] overflow-x-auto pb-1" style={{ height }}>
      {items.map((it, idx) => {
        const h = Math.max(2, (Math.abs(it.value) / max) * (height - 4));
        return (
          <div
            key={idx}
            className="flex flex-col items-center justify-end shrink-0"
            style={{ width: 6, height }}
            title={`${it.label}: ${it.value}`}
          >
            <div className={`w-full rounded-[2px] ${it.value >= 0 ? 'bg-green' : 'bg-red'}`} style={{ height: h }} />
          </div>
        );
      })}
    </div>
  );
}

/* Cumulative equity curve + drawdown shading, from a chronological list of
   trade net P&Ls. Hand-rolled inline SVG — no charting library in this app. */
export function EquityCurve({ trades = [], height = 140 }) {
  if (!trades || trades.length === 0) return null;

  let running = 0;
  let peak = 0;
  const points = trades.map((t) => {
    running += Number(t.net_pnl) || 0;
    peak = Math.max(peak, running);
    return { equity: running, drawdown: peak - running };
  });

  const width = 600;
  const maxEquity = Math.max(0, ...points.map((p) => p.equity));
  const minEquity = Math.min(0, ...points.map((p) => p.equity));
  const span = maxEquity - minEquity || 1;
  const n = points.length;
  const x = (i) => (n === 1 ? width / 2 : (i / (n - 1)) * width);
  const y = (v) => height - ((v - minEquity) / span) * height;

  const linePath = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(p.equity).toFixed(1)}`).join(' ');
  const zeroY = y(0).toFixed(1);
  const areaPath = `${linePath} L ${x(n - 1).toFixed(1)} ${zeroY} L ${x(0).toFixed(1)} ${zeroY} Z`;

  const finalEquity = points[points.length - 1].equity;
  const lineColor = finalEquity >= 0 ? '#00D98B' : '#FF4D5E';

  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="w-full" style={{ height }}>
      <defs>
        <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={lineColor} stopOpacity="0.22" />
          <stop offset="100%" stopColor={lineColor} stopOpacity="0" />
        </linearGradient>
      </defs>
      <line x1="0" y1={zeroY} x2={width} y2={zeroY} stroke="#232329" strokeWidth="1" strokeDasharray="4,4" />
      <path d={areaPath} fill="url(#eqfill)" />
      <path d={linePath} fill="none" stroke={lineColor} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function InstitutionalPostureCard({ instData, instrument = 'NIFTY', spot = 0 }) {
  if (!instData) return null;

  const regime = instData.regime || 'NEUTRAL';
  const callWall = instData.call_wall || 0;
  const putWall = instData.put_wall || 0;
  const pcr = instData.pcr || 1.0;
  const pcrSentiment = instData.pcr_sentiment || 'NORMAL';
  const distCall = instData.dist_call_wall_pct ?? 0;
  const distPut = instData.dist_put_wall_pct ?? 0;

  const regimeBadge = {
    LONG_BUILDUP: { label: 'Long Buildup', color: '!bg-green-soft !text-green !border-green/40' },
    SHORT_BUILDUP: { label: 'Short Buildup', color: '!bg-red-soft !text-red !border-red/40' },
    SHORT_COVERING: { label: 'Short Covering', color: '!bg-amber-soft !text-amber !border-amber/40' },
    LONG_UNWINDING: { label: 'Long Unwinding', color: '!bg-amber-soft !text-amber !border-amber/40' },
    NEUTRAL: { label: 'Neutral', color: '' },
  }[regime] || { label: regime, color: '' };

  const pcrBadge = {
    FEAR_OVERSOLD: { label: 'Fear / oversold', color: 'text-green' },
    COMPLACENT_OVERBOUGHT: { label: 'Complacent / exhaustion', color: 'text-red' },
    NORMAL: { label: 'Balanced', color: 'text-ink-2' },
  }[pcrSentiment] || { label: pcrSentiment, color: 'text-ink-2' };

  const span = callWall - putWall;
  let pct = 50;
  if (span > 0 && spot > 0) pct = Math.min(100, Math.max(0, ((spot - putWall) / span) * 100));

  return (
    <Card className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>Institutional posture · {instrument}</Eyebrow>
        <span className={`chip font-semibold ${regimeBadge.color}`}>{regimeBadge.label}</span>
      </div>

      <div className="space-y-2">
        <div className="flex justify-between items-baseline num text-sec">
          <span className="text-muted">
            put wall <b className="text-ink font-medium">{putWall ? putWall.toLocaleString('en-IN') : '—'}</b>
            {distPut > 0 ? <span className="text-muted"> +{distPut}%</span> : ''}
          </span>
          <span className="text-ink font-medium">{spot ? spot.toLocaleString('en-IN') : ''}</span>
          <span className="text-muted">
            call wall <b className="text-ink font-medium">{callWall ? callWall.toLocaleString('en-IN') : '—'}</b>
            {distCall > 0 ? <span className="text-muted"> −{distCall}%</span> : ''}
          </span>
        </div>

        <div className="relative h-2 bg-well border border-line/70 rounded-pill overflow-hidden">
          <div className="absolute inset-y-0 left-0 bg-line-soft" style={{ width: `${pct}%` }} />
          <div
            className="absolute top-1/2 -translate-y-1/2 w-2.5 h-2.5 bg-ai rounded-full -ml-1.5"
            style={{ left: `${pct}%`, boxShadow: '0 0 10px rgba(77,141,255,0.6)' }}
          />
        </div>
      </div>

      <div className="flex justify-between items-center num text-eyebrow pt-2.5 border-t border-line">
        <span className="text-muted">
          PCR <b className="text-ink text-sec font-medium">{pcr != null ? pcr.toFixed(2) : '—'}</b>
        </span>
        <span className={`font-medium ${pcrBadge.color}`}>{pcrBadge.label}</span>
      </div>
    </Card>
  );
}
