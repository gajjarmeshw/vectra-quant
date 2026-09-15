/* Shared components. Visual contracts from docs/design_spec.md §2.
   Colour only ever carries meaning: green money, red risk, amber guardian,
   cobalt AI. Everything else is paper/ink/hairline. */
import React, { useState } from 'react';
import { motion } from 'framer-motion';
import { Lock, Target, TrendingUp, AlertCircle, Eye, EyeOff, FileWarning, BarChart2 } from 'lucide-react';

export const rupee = (n, sign = false) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const v = Math.round(Math.abs(n)).toLocaleString('en-IN');
  const s = n < 0 ? '−' : sign ? '+' : '';
  return `${s}₹${v}`;
};

export const num = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n) ? '—' : Number(n).toFixed(d);

export const pnlColor = (n) => (n > 0 ? 'text-green' : n < 0 ? 'text-red' : 'text-ink');

export function Eyebrow({ children }) {
  return <div className="eyebrow">{children}</div>;
}

export function Card({ children, className = '', accent = false }) {
  return (
    <div
      className={`card ${className}`}
      style={accent ? { borderColor: '#2447F5', borderWidth: 1.5 } : undefined}
    >
      {children}
    </div>
  );
}

/* The FSM made visible. Always current, top-right of every screen. */
export function StateChip({ state, floor }) {
  const base = 'chip font-medium bg-line-soft border-line/50 text-white';
  if (state === 'LOCKED') return <span className={`${base} !bg-red/20 !text-red !border-red/50`}><Lock size={12} className="inline mr-1" />LOCKED</span>;
  if (state === 'EARNED')
    return <span className={`${base} !bg-ai/20 !text-ai !border-ai/50`}><Target size={12} className="inline mr-1" />EARNED</span>;
  if (state === 'PROTECT') return <span className={`${base} !bg-amber/20 !text-amber !border-amber/50`}><AlertCircle size={12} className="inline mr-1" />PROTECT</span>;
  if (state === 'TRAIL')
    return (
      <span className={`${base} !bg-green/20 !text-green !border-green/50`}><TrendingUp size={12} className="inline mr-1" />TRAIL</span>
    );
  return <span className={`${base} text-ink-2`}>NORMAL</span>;
}

export function OriginTag({ origin, confidence }) {
  if (origin === 'manual') {
    return <span className="chip bg-amber-soft text-amber border-amber-soft">MANUAL</span>;
  }
  return (
    <span className="chip bg-ai-soft text-ai border-ai-soft">
      AI{confidence ? ` · ${confidence}` : ''}
    </span>
  );
}

/* Day Rail — the signature component (design_spec §2).
   Hatched red zone left of the lock, ticks for floor / target / trail-on, and a
   live dot that moves with P&L. The floor tick MOVES as the floor ratchets. */
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
      <div className="relative h-2.5 rounded-pill bg-line-soft overflow-visible shadow-inner">
        <div
          className="absolute inset-y-0 left-0 rounded-l-pill bg-red-soft"
          style={{
            width: `${lockPct}%`,
            backgroundImage:
              'repeating-linear-gradient(45deg,rgba(255,61,0,0.1) 0 4px,rgba(255,61,0,0.2) 4px 8px)',
          }}
        />
        <div className="absolute -top-1.5 w-[2px] h-5 bg-ink " style={{ left: `${floorPct}%` }} />
        <div className="absolute -top-0.5 w-[2px] h-3.5 bg-muted" style={{ left: `${targetPct}%` }} />
        <div className="absolute -top-0.5 w-[2px] h-3.5 bg-muted" style={{ left: `${trailPct}%` }} />
        <motion.div
          className="absolute rounded-full border-2 border-paper"
          layout
          transition={{ type: "spring", stiffness: 100, damping: 20 }}
          style={{
            left: `calc(${dotPct}% - 9px)`,
            top: '-4px',
            width: 18,
            height: 18,
            background: dayPnl >= 0 ? '#10B981' : '#EF4444',
          }}
        />
      </div>
      <div className="num text-eyebrow text-muted flex justify-between mt-3">
        <span>lock {rupee(-Math.abs(lossLimit))}</span>
        <span className="text-white">floor {rupee(floor)}</span>
        <span>target {rupee(target)}</span>
        <span>trail {rupee(target * 1.3)}</span>
      </div>
    </div>
  );
}

/* Distance-to-SL bar (design_spec §2, mockup screen 03). Red→green gradient with a
   mark at the stop, a mark at the target, and the live premium riding between them.
   Without a target set the bar still shows how close the stop is. */
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
    <div className="mt-5">
      <div
        className="relative h-1.5 rounded-pill"
        style={{ background: 'linear-gradient(90deg, rgba(255,61,0,0.3), rgba(255,255,255,0.1) 40%, rgba(0,230,118,0.3))' }}
      >
        <span className="absolute -top-[5px] w-0.5 h-4 rounded-sm bg-red " style={{ left: '0%' }} />
        {tgt > stop && (
          <span
            className="absolute -top-[5px] w-0.5 h-4 rounded-sm bg-green "
            style={{ left: '100%' }}
          />
        )}
        <motion.span
          className="absolute rounded-full bg-paper border-[3px] border-ink"
          layout
          transition={{ type: "spring", stiffness: 120, damping: 20 }}
          style={{
            left: `calc(${pct(px)}% - 7px)`,
            top: '-4px',
            width: 14,
            height: 14,
          }}
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
          width: 10,
          height: 10,
          background: used ? '#0B0B0A' : 'transparent',
          border: used
            ? 'none'
            : earned && cap <= base
              ? '1px dashed #8A8A85'
              : '1px solid #0B0B0A',
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
      <div className={`num text-contract mt-1 ${tone}`}>{value}</div>
      {sub && <div className="num text-sec text-muted mt-0.5">{sub}</div>}
    </div>
  );
}

export function Row({ left, right, className = '' }) {
  return (
    <div className={`flex items-center justify-between ${className}`}>
      <div>{left}</div>
      <div className="text-right">{right}</div>
    </div>
  );
}

export function Empty({ children }) {
  return <div className="text-center text-muted text-body py-10">{children}</div>;
}

export function Banner({ tone = 'amber', children }) {
  const map = {
    amber: 'bg-amber-soft text-amber',
    red: 'bg-red-soft text-red',
    ink: 'bg-line-soft text-ink-2',
    ai: 'bg-ai-soft text-ai',
  };
  return (
    <div className={`num text-chip text-center py-2 px-3 rounded-block ${map[tone]}`}>
      {children}
    </div>
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
    LONG_BUILDUP: { label: 'Long Buildup · Whale Buying', color: 'bg-green-soft text-green border-green-soft' },
    SHORT_BUILDUP: { label: 'Short Buildup · Whale Selling', color: 'bg-red-soft text-red border-red-soft' },
    SHORT_COVERING: { label: 'Short Covering · Fades Fast', color: 'bg-amber-soft text-amber border-amber-soft' },
    LONG_UNWINDING: { label: 'Long Unwinding · Weak Drift', color: 'bg-amber-soft text-amber border-amber-soft' },
    NEUTRAL: { label: 'Neutral · Consolidation', color: 'bg-line-soft text-ink-2 border-line' },
  }[regime] || { label: regime, color: 'bg-line-soft text-ink-2 border-line' };

  const pcrBadge = {
    FEAR_OVERSOLD: { label: 'Fear / Oversold (<0.7)', color: 'text-green' },
    COMPLACENT_OVERBOUGHT: { label: 'Complacent / Exhaustion (>1.3)', color: 'text-red' },
    NORMAL: { label: 'Balanced (0.7–1.3)', color: 'text-ink-2' },
  }[pcrSentiment] || { label: pcrSentiment, color: 'text-ink-2' };

  // Calculate spot position percentage between Put Wall (0%) and Call Wall (100%)
  const span = callWall - putWall;
  let pct = 50;
  if (span > 0 && spot > 0) {
    pct = Math.min(100, Math.max(0, ((spot - putWall) / span) * 100));
  }

  return (
    <Card className="!p-3.5 space-y-2.5 border border-line">
      <div className="flex items-center justify-between">
        <Eyebrow>Institutional Shadowing · {instrument}</Eyebrow>
        <span className={`chip font-medium text-[11px] px-2 py-0.5 rounded-full border ${regimeBadge.color}`}>
          {regimeBadge.label}
        </span>
      </div>

      {/* Expected Range Track: Put Wall -> Spot -> Call Wall */}
      <div className="space-y-1.5 pt-1">
        <div className="flex justify-between items-center text-[12px] font-mono">
          <span className="text-muted">
            Floor: <b className="text-ink">{putWall ? putWall.toLocaleString('en-IN') : '—'}</b>
            {distPut > 0 ? ` (+${distPut}%)` : ''}
          </span>
          <span className="text-muted font-semibold text-ink">
            {spot ? spot.toLocaleString('en-IN') : ''}
          </span>
          <span className="text-muted">
            Ceiling: <b className="text-ink">{callWall ? callWall.toLocaleString('en-IN') : '—'}</b>
            {distCall > 0 ? ` (-${distCall}%)` : ''}
          </span>
        </div>

        <div className="relative h-2 bg-line rounded-full overflow-hidden">
          <div
            className="absolute top-0 bottom-0 left-0 bg-line-soft"
            style={{ width: `${pct}%` }}
          />
          <div
            className="absolute top-0 bottom-0 w-2 bg-ai rounded-full -ml-1 shadow-sm"
            style={{ left: `${pct}%` }}
          />
        </div>
      </div>

      {/* PCR Metric & Sentiment */}
      <div className="flex justify-between items-center text-[11px] text-muted pt-1 border-t border-line">
        <span>
          Put-Call Ratio (PCR): <b className="font-mono text-ink text-[12px]">{pcr != null ? pcr.toFixed(2) : '—'}</b>
        </span>
        <span className={`font-medium ${pcrBadge.color}`}>
          {pcrBadge.label}
        </span>
      </div>
    </Card>
  );
}

