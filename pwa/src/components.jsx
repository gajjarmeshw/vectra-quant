/* Shared components. Visual contracts from docs/design_spec.md §2.
   Colour only ever carries meaning: green money, red risk, amber guardian,
   cobalt AI. Everything else is paper/ink/hairline. */
import React from 'react';

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
  const base = 'chip font-medium';
  if (state === 'LOCKED') return <span className={`${base} bg-red text-white border-red`}>LOCKED</span>;
  if (state === 'EARNED')
    return <span className={`${base} bg-ink text-white border-ink`}>EARNED · 4th unlocked</span>;
  if (state === 'PROTECT') return <span className={`${base} bg-ink text-white border-ink`}>PROTECT</span>;
  if (state === 'TRAIL')
    return (
      <span className={`${base} bg-ink text-white border-ink`}>TRAIL · floor {rupee(floor)}</span>
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
      <div className="relative h-2 rounded-pill bg-line-soft overflow-visible">
        <div
          className="absolute inset-y-0 left-0 rounded-l-pill bg-red-soft"
          style={{
            width: `${lockPct}%`,
            backgroundImage:
              'repeating-linear-gradient(45deg,#FDEBEC 0 4px,#FBD5D7 4px 8px)',
          }}
        />
        <div className="absolute -top-1.5 w-px h-5 bg-ink" style={{ left: `${floorPct}%` }} />
        <div className="absolute -top-0.5 w-px h-3 bg-muted" style={{ left: `${targetPct}%` }} />
        <div className="absolute -top-0.5 w-px h-3 bg-muted" style={{ left: `${trailPct}%` }} />
        <div
          className="absolute rounded-full border-2 border-white transition-all duration-700"
          style={{
            left: `calc(${dotPct}% - 9px)`,
            top: '-5px',
            width: 18,
            height: 18,
            background: dayPnl >= 0 ? '#0E9F6E' : '#E5484D',
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
        style={{ background: 'linear-gradient(90deg,#FDEBEC,#F2F2EF 40%,#E6F6F0)' }}
      >
        <span className="absolute -top-[5px] w-0.5 h-4 rounded-sm bg-red" style={{ left: '0%' }} />
        {tgt > stop && (
          <span
            className="absolute -top-[5px] w-0.5 h-4 rounded-sm bg-green"
            style={{ left: '100%' }}
          />
        )}
        <span
          className="absolute rounded-full bg-ink border-[3px] border-white transition-all duration-500"
          style={{
            left: `calc(${pct(px)}% - 7px)`,
            top: '-4px',
            width: 14,
            height: 14,
            boxShadow: '0 1px 4px rgba(0,0,0,.3)',
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
