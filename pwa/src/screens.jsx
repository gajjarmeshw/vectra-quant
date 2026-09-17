/* The seven screens. docs/design_spec.md §3. */
import React, { useEffect, useRef, useState } from 'react';
import { CheckCircle2, Percent, Layers } from 'lucide-react';
import {
  Banner, Card, DayRail, Empty, Eyebrow, OriginTag, Row, SlTrack, Stat, StateChip,
  TradeDots, num, pnlColor, rupee, InstitutionalPostureCard,
  ProgressRail, EquityCurve, MiniBars, SectionHeader, StatTile, Delta, Sparkline,
  AnimatedRupee, Skeleton, SkeletonCard,
} from './components.jsx';
import * as api from './api.js';

/* Each strategy family gets a stable colour so it's recognisable wherever it
   appears — catalog card, order-flow panel, backtest picker. */
const STRATEGY_ACCENT = {
  breadth: 'cyan',
  thunderbolt: 'violet',
  weekly_credit_spread: 'orange',
  renko_strategy: 'teal',
};

const ACCENT_CLASSES = {
  cyan: { bg: 'bg-cyan', text: 'text-cyan', border: 'border-cyan/50', chip: '!bg-cyan-soft !text-cyan !border-cyan/40' },
  violet: { bg: 'bg-violet', text: 'text-violet', border: 'border-violet/50', chip: '!bg-violet-soft !text-violet !border-violet/40' },
  orange: { bg: 'bg-orange', text: 'text-orange', border: 'border-orange/50', chip: '!bg-orange-soft !text-orange !border-orange/40' },
  teal: { bg: 'bg-teal', text: 'text-teal', border: 'border-teal/50', chip: '!bg-teal-soft !text-teal !border-teal/40' },
  ai: { bg: 'bg-ai', text: 'text-ai', border: 'border-ai/50', chip: '!bg-ai-soft !text-ai !border-ai/40' },
};

/* IST wall-clock from an ISO timestamp — the phone may be anywhere. */
const hhmm = (iso) =>
  new Date(iso).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata',
  });

/* ---------------------------------------------------------------- Today */

export function Today({ s, onSquareOff }) {
  const fsm = s.fsm || {};
  const closed = (s.trades || []).filter((t) => t.status === 'CLOSED');
  const wins = closed.filter((t) => (t.pnl || 0) > 0).length;

  return (
    <div className="space-y-cardgap">
      {/* Hero: the one number that matters, with the rail underneath it. */}
      <Card
        tone={fsm.day_pnl > 0 ? 'green' : fsm.day_pnl < 0 ? 'red' : ''}
        wash={fsm.day_pnl > 0 ? 'green' : fsm.day_pnl < 0 ? 'red' : 'ai'}
      >
        <Row
          left={<Eyebrow>Day P&amp;L</Eyebrow>}
          right={<StateChip state={fsm.state} />}
        />
        <div className={`text-hero mt-2 font-medium ${pnlColor(fsm.day_pnl)}`}>
          <AnimatedRupee value={fsm.day_pnl} />
        </div>
        {fsm.loss_limit != null && (
          <DayRail
            dayPnl={fsm.day_pnl || 0}
            floor={fsm.floor || 0}
            target={fsm.target || 0}
            lossLimit={fsm.loss_limit || 0}
          />
        )}
        <div className="grid grid-cols-3 gap-2 mt-4">
          <StatTile label="Closed" value={closed.length} accent="violet" icon={CheckCircle2} />
          <StatTile
            label="Win rate"
            value={closed.length ? `${Math.round((wins / closed.length) * 100)}%` : '—'}
            tone={closed.length && wins / closed.length >= 0.5 ? 'text-green' : ''}
            accent="cyan"
            icon={Percent}
          />
          <StatTile
            label="Open"
            value={(s.positions || []).length}
            accent="orange"
            icon={Layers}
          />
        </div>
      </Card>

      {s.expiry_today?.length > 0 && (
        <Banner tone="amber">
          EXPIRY DAY · {s.expiry_today.join(', ')} — entries close 14:30
        </Banner>
      )}

      <PremarketCard />

      <ClosedTradesTable title="Closed today" rows={closed} empty="No closed trades yet." />

      <StatusStrip s={s} />
    </div>
  );
}

/* The single rendering of a fill list. Home feeds it live state, Reports feeds
   it the archived day-end replay — previously two divergent layouts of the
   same seven columns. */
export function ClosedTradesTable({ title, rows = [], empty = 'Nothing here yet.' }) {
  const net = rows.reduce((a, t) => a + (t.pnl || 0), 0);
  return (
    <Card className="!p-0 overflow-hidden">
      <div className="px-cardpad pt-cardpad pb-2.5 flex items-center justify-between">
        <Eyebrow>{title}</Eyebrow>
        {rows.length > 0 && (
          <span className={`num text-sec font-medium ${pnlColor(net)}`}>{rupee(net, true)}</span>
        )}
      </div>
      {rows.length === 0 ? (
        <div className="px-cardpad pb-cardpad">
          <Empty>{empty}</Empty>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="tbl">
            <thead>
              <tr>
                <th>Time</th>
                <th>Symbol</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Exit</th>
                <th>Reason</th>
                <th className="text-right">Charges</th>
                <th className="text-right">Net</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t, i) => (
                <tr key={t.id ?? i}>
                  <td className="whitespace-nowrap">{t.closed_at ? hhmm(t.closed_at) : '—'}</td>
                  <td className="text-ink font-medium whitespace-nowrap">{t.symbol}</td>
                  <td className="text-right">{num(t.entry)}</td>
                  <td className="text-right">{num(t.exit)}</td>
                  <td className="whitespace-nowrap">{t.reason || '—'}</td>
                  <td className="text-right text-muted">{rupee(t.costs)}</td>
                  <td className={`text-right font-medium ${pnlColor(t.pnl)}`}>{rupee(t.pnl, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function StatusStrip({ s }) {
  if (s.fsm?.state === 'LOCKED') return <Banner tone="red">LOCKED · {s.fsm.lock_reason}</Banner>;
  if (s.kill_switch === 'OFF') return <Banner tone="red">KILL SWITCH OFF · no suggestions</Banner>;
  if (s.health?.feed_degraded)
    return <Banner tone="amber">DATA DEGRADED — suggestions paused</Banner>;
  if (!s.in_entry_window) return <Banner tone="ink">Outside entry window</Banner>;
  return (
    <Banner tone="ink">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-ai mr-2 ai-pulse" />
      Waiting for next signal · LLM armed
    </Banner>
  );
}

/* ---------------------------------------------------------------- Signals */

export function AlgoStrategyBanner({ algo }) {
  if (!algo) return null;
  const stratNames = {
    institutional_breakout: 'Institutional Breakout (ORB + Walls)',
    option_wall_squeeze: 'Trapped Option Wall Squeeze',
    wall_mean_reversion: 'Defended Range Mean Reversion',
  };
  const activeLabel = stratNames[algo.active_strategy] || algo.active_strategy || 'Default Strategy';
  const isAuto = Boolean(algo.auto_execute);

  return (
    <Card className="!p-3.5 space-y-1.5 border border-line">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className={`inline-block w-2 h-2 rounded-full ${algo.enabled ? 'bg-ai ai-pulse' : 'bg-muted'}`} />
          <Eyebrow>Active Algo Strategy</Eyebrow>
        </div>
        <span
          className={`chip font-semibold text-f11 px-2 py-0.5 rounded-full border ${
            isAuto ? 'bg-ai-soft text-ai border-ai-soft' : 'bg-line-soft text-ink-2 border-line'
          }`}
        >
          {isAuto ? '⚡ Auto-Pilot' : '✋ Assisted'}
        </span>
      </div>
      <div className="font-disp text-f15 font-semibold text-ink">
        {activeLabel}
      </div>
      <div className="num text-sec text-muted">
        {isAuto
          ? 'Autonomous execution active · Validated algo signals execute directly through FSM risk gates.'
          : 'Assisted mode · Algo signals queue below for 1-tap human execution.'}
      </div>
    </Card>
  );
}

export function Signals({ s, onApprove, onReject, busy }) {
  const active = s.active_suggestions || [];
  return (
    <div className="space-y-cardgap">
      <AlgoStrategyBanner algo={s.algo} />

      {active.map((q) => (
        <SuggestionCard
          key={q.id}
          q={q}
          onApprove={onApprove}
          onReject={onReject}
          busy={busy === q.id}
        />
      ))}

      <Card>
        <Eyebrow>Today&rsquo;s signals</Eyebrow>
        {(s.suggestions || []).length === 0 ? (
          <Empty>
            No signals yet. Detectors armed: level break, VIX, OI shift, momentum.
          </Empty>
        ) : (
          <div className="mt-3 space-y-3">
            {(s.suggestions || []).map((q) => (
              <Row
                key={q.id}
                className="pb-3 border-b border-line last:border-0"
                left={
                  <>
                    <div className="num text-sec text-muted">{q.event}</div>
                    <div className="num text-body mt-0.5">
                      {q.instrument} {q.strike ? Math.round(q.strike) : ''} {q.direction}
                    </div>
                  </>
                }
                right={<VerdictChip q={q} />}
              />
            ))}
          </div>
        )}
        <div className="num text-eyebrow text-muted mt-4 pt-3 border-t border-line">
          Suggestions {s.llm?.calls_today ?? 0}/{s.llm?.cap ?? 10} ·{' '}
          {Object.entries(s.llm?.by_provider || {})
            .map(([k, v]) => `${k} ${v}`)
            .join(' · ') || 'no calls yet'}
        </div>
      </Card>
    </div>
  );
}

function VerdictChip({ q }) {
  const map = {
    TAKEN: 'bg-ink text-paper border-ink',
    REJECTED: 'text-ink-2',
    EXPIRED: 'text-muted',
    NO_TRADE: 'text-ai',
    GATED: 'bg-amber-soft text-amber border-amber-soft',
    QUEUED: 'bg-ai-soft text-ai border-ai-soft',
  };
  return (
    <span className={`chip ${map[q.status] || ''}`} title={q.gate_reason || ''}>
      {q.status}
      {q.status === 'GATED' && q.gate_reason ? ` · ${q.gate_reason}` : ''}
    </span>
  );
}

function SuggestionCard({ q, onApprove, onReject, busy }) {
  const [left, setLeft] = useState(q.seconds_left ?? 0);
  useEffect(() => {
    setLeft(q.seconds_left ?? 0);
    const t = setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000);
    return () => clearInterval(t);
  }, [q.id, q.seconds_left]);

  const dead = left <= 0;
  const sideColor = q.direction === 'CE' ? 'text-green' : 'text-red';
  const isAlgo = q.origin === 'ALGO' || (q.model && q.model.startsWith('algo:'));

  return (
    <Card accent className={dead ? 'opacity-50' : ''}>
      <div className="-m-cardpad mb-cardpad px-cardpad py-2.5 bg-ai-soft rounded-t-card flex justify-between items-center">
        <span className="num text-eyebrow text-ai flex items-center gap-2">
          <span className="inline-block w-[7px] h-[7px] rounded-full bg-ai ai-pulse" />
          {isAlgo ? (
            <span className="font-semibold tracking-wider text-ai">
              ALGO · {(q.model || '').replace('algo:', '').replace(/_/g, ' ').toUpperCase()}
            </span>
          ) : (
            <>
              {(q.model || 'LLM').split('/').pop().toUpperCase()}
              {q.event ? ` · ${q.event}` : ''}
            </>
          )}
        </span>
        <span className="num text-eyebrow text-ai">
          {dead ? 'expired — market moved' : `expires 0:${String(left).padStart(2, '0')}`}
        </span>
      </div>

      <div className={`font-disp text-contract font-semibold ${sideColor}`}>
        {q.instrument} {Math.round(q.strike)} {q.direction}
      </div>
      <div className="num text-sec text-muted mt-0.5">{q.symbol}</div>

      <p className="text-body text-ink-2 mt-3 pl-3 border-l-2 border-line">
        {q.thesis}
        {q.invalidation && (
          <>
            <br />
            <span className="text-muted">Invalidation: {q.invalidation}</span>
          </>
        )}
      </p>

      <div className="grid grid-cols-2 gap-3 mt-4">
        <Stat label="Entry zone" value={`${num(q.entry_low)}–${num(q.entry_high)}`} />
        <Stat label="Stop" value={num(q.sl)} tone="text-red" />
        <Stat label="Target" value={num(q.target)} tone="text-green" />
        <Stat label="Time stop" value={`${q.time_stop}m`} />
      </div>

      <div className="mt-4">
        <Row
          left={<Eyebrow>Confidence</Eyebrow>}
          right={<span className="num text-body text-ai">{q.confidence}</span>}
        />
        <div className="h-1.5 rounded-pill bg-line-soft mt-1.5 overflow-hidden">
          <div className="h-full bg-ai" style={{ width: `${q.confidence}%` }} />
        </div>
      </div>

      <div className="num text-sec text-ink-2 mt-4 p-3 rounded-block bg-line-soft">
        {q.lots} lot · risk {rupee(q.risk)} · cost {rupee(q.cost)} · RR {num(q.rr, 2)}
        {q.over_risk && <span className="text-amber"> · over-risk</span>}
      </div>

      <div className="flex gap-2 mt-4">
        <button className="btn-ghost flex-1" disabled={dead || busy} onClick={() => onReject(q.id)}>
          Reject
        </button>
        <button
          className="btn-primary flex-[2]"
          disabled={dead || busy}
          onClick={() => onApprove(q.id)}
        >
          {busy ? 'Placing…' : `Approve · buy ${q.lots} lot`}
        </button>
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- Positions */

export function Positions({ s, onSquareOff, busy }) {
  const ps = s.positions || [];
  const totalUnreal = ps.reduce((a, p) => a + (Number(p.unrealized) || 0), 0);

  if (ps.length === 0) {
    return (
      <div>
        <SectionHeader title="Open positions" />
        <Empty>Flat. Nothing at risk.</Empty>
      </div>
    );
  }
  const openTrades = (s.trades || []).filter((t) => t.status === 'OPEN');
  return (
    <div className="space-y-cardgap">
      <SectionHeader
        title="Open positions"
        sub={`${ps.length} held`}
        right={
          <span className={`num text-contract font-medium ${pnlColor(totalUnreal)}`}>
            {rupee(totalUnreal, true)}
          </span>
        }
      />
      {ps.map((p) => {
        const t = openTrades.find((x) => x.symbol === p.symbol);
        return (
          <Card key={p.symbol}>
            <Row
              left={
                <Eyebrow>
                  {p.symbol}
                  {p.lot_size ? ` · ${Math.max(1, Math.round(Math.abs(p.qty) / p.lot_size))} lot (${p.lot_size})` : ''}
                </Eyebrow>
              }
              right={t ? <OriginTag origin={t.origin} /> : null}
            />
            <div className={`num text-lock mt-2 ${pnlColor(p.unrealized)}`}>
              {rupee(p.unrealized, true)}
            </div>
            <div className="num text-sec text-muted mt-1">
              avg {num(p.avg)} → ltp {num(p.ltp)} · {p.qty} qty
            </div>
            {p.est_charges != null && (
              <div className="num text-sec text-muted mt-1">
                after charges {rupee(p.net_unrealized, true)}
                <span className="text-muted"> · {rupee(p.est_charges)} to exit</span>
              </div>
            )}
            <SlTrack sl={t?.sl} target={t?.target} ltp={p.ltp} entry={p.avg} />
            {t?.guardian_note && (
              <div className="flex gap-2.5 items-start mt-3.5 pt-3 border-t border-dashed border-line">
                <span className="shrink-0 w-[22px] h-[22px] rounded-full bg-amber-soft text-amber grid place-items-center text-f11">
                  ⛨
                </span>
                <span className="text-sec text-ink-2">
                  <b>Guardian:</b> {t.guardian_note}
                </span>
              </div>
            )}
          </Card>
        );
      })}
      <button className="btn-danger" disabled={busy} onClick={onSquareOff}>
        {busy ? 'Exiting…' : 'Square off everything'}
      </button>
      <div className="num text-eyebrow text-muted text-center">
        Market exit · verifies flat within 10s
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- Locked */

/* Counts down to the next 09:15 IST in the phone's own clock. */
function useUnlockCountdown() {
  const [txt, setTxt] = useState('');
  useEffect(() => {
    const tick = () => {
      const now = new Date();
      // IST regardless of where the phone thinks it is.
      const ist = new Date(now.getTime() + (330 + now.getTimezoneOffset()) * 60000);
      const open = new Date(ist);
      open.setHours(9, 15, 0, 0);
      if (ist >= open) open.setDate(open.getDate() + 1);
      while (open.getDay() === 0 || open.getDay() === 6) open.setDate(open.getDate() + 1);
      const secs = Math.max(0, Math.floor((open - ist) / 1000));
      const p = (n) => String(n).padStart(2, '0');
      setTxt(`${p(Math.floor(secs / 3600))}:${p(Math.floor((secs % 3600) / 60))}:${p(secs % 60)}`);
    };
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, []);
  return txt;
}

export function Locked({ s, onReadReport }) {
  const fsm = s.fsm || {};
  const countdown = useUnlockCountdown();
  const green = (fsm.day_pnl ?? 0) >= 0;
  return (
    <div className="space-y-cardgap">
      <div className="text-center pt-8 pb-4">
        <div
          className="w-[104px] h-[104px] mx-auto mb-6 rounded-full grid place-items-center text-lock"
          style={{ background: green ? 'rgb(var(--c-green) / 0.14)' : 'rgb(var(--c-red) / 0.14)' }}
        >
          {green ? '🛡' : '🛑'}
        </div>
        <h2 className="font-disp text-lock font-bold tracking-tight">Done for today.</h2>
        <p className="text-body text-ink-2 mt-2.5 leading-relaxed">
          {fsm.lock_reason || 'The day is locked.'}
          <br />
          Entries reopen next session.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2.5">
        <div className="card !p-3.5">
          <Eyebrow>Day result</Eyebrow>
          <div className={`num text-contract font-semibold mt-1.5 ${pnlColor(fsm.day_pnl)}`}>
            {rupee(fsm.day_pnl, true)}
          </div>
        </div>
        <div className="card !p-3.5">
          <Eyebrow>Unlocks in</Eyebrow>
          <div className="num text-contract font-semibold mt-1.5">{countdown}</div>
        </div>
      </div>

      <PremarketCard />

      <button className="btn-primary" onClick={onReadReport}>
        Read day-end report
      </button>
      <a
        className="btn-ghost block text-center"
        href={s.broker === 'dhan' ? 'https://web.dhan.co/' : 'https://groww.in/user/profile/settings'}
        target="_blank"
        rel="noreferrer"
      >
        {s.broker === 'dhan' ? 'DhanHQ Portal ↗' : 'Groww kill switch ↗'}
      </a>

      <Banner tone="ink">
        {(s.positions || []).length > 0
          ? `Your broker-side stops remain resting on ${s.broker === 'dhan' ? 'DhanHQ' : 'Groww'}.`
          : 'All positions flat. Zero active resting orders on exchange.'}
      </Banner>
    </div>
  );
}

/* ---------------------------------------------------------------- Journal */

export function Journal() {
  const [j, setJ] = useState(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    api.getJournal().then(setJ).catch((e) => setErr(e.message));
  }, []);
  if (err) return <Card><Empty>{err}</Empty></Card>;
  if (!j) return <SkeletonCard rows={4} />;

  const st = j.stats || {};
  const cal = j.calibration || {};
  return (
    <div className="space-y-cardgap">
      <Card>
        <Eyebrow>Stats</Eyebrow>
        <div className="grid grid-cols-2 gap-4 mt-3">
          <Stat label="Expectancy" value={rupee(st.expectancy, true)} sub="per trade" />
          <Stat label="Win rate" value={`${num(st.win_pct, 1)}%`} sub={`${st.trades} trades`} />
          <Stat label="Profit factor" value={st.profit_factor ?? '—'} />
          <Stat
            label="Avg win / loss"
            value={`${rupee(st.avg_win)} / ${rupee(st.avg_loss)}`}
          />
        </div>
        <div className="num text-sec text-muted mt-4 pt-3 border-t border-line">
          Net {rupee(st.net, true)} · costs paid {rupee(st.total_costs)}
        </div>
      </Card>

      <Card>
        <Eyebrow>Calibration</Eyebrow>
        <table className="w-full num text-sec mt-3">
          <thead>
            <tr className="text-ai text-eyebrow uppercase">
              <th className="text-left font-normal pb-2">Bucket</th>
              <th className="text-right font-normal pb-2">Hit %</th>
              <th className="text-right font-normal pb-2">n</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(cal.buckets || {}).map(([k, v]) => (
              <tr key={k} className="border-t border-line">
                <td className="py-2">{k}</td>
                <td className="text-right">{v.hit_pct ?? '—'}</td>
                <td className="text-right text-muted">{v.n}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {cal.kill_criterion_tripped && (
          <div className="mt-3">
            <Banner tone="red">{cal.recommendation}</Banner>
          </div>
        )}
      </Card>

      <Card>
        <Eyebrow>Violations</Eyebrow>
        {(j.violations || []).length === 0 ? (
          <Empty>No violations. Clean record.</Empty>
        ) : (
          <div className="mt-3 space-y-2">
            {j.violations.map((v, i) => (
              <Row
                key={i}
                className="text-amber"
                left={<span className="num text-sec">{v.date} · {v.kind}</span>}
                right={<span className="num text-sec">{rupee(v.cost)}</span>}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- Reports */

/* One destination for everything retrospective. The day-end verdict and the
   rolling journal used to be reachable from four different buttons (Today,
   Locked, System, and a full-screen overlay) — they are two views of the same
   archive, so they live behind one segmented control here. */
const REPORT_VIEWS = [
  { id: 'dayend', label: 'Day-end' },
  { id: 'journal', label: 'Journal' },
];

export function Reports() {
  const [view, setView] = useState('dayend');
  return (
    <div className="space-y-cardgap">
      <div className="well p-1 inline-flex gap-1">
        {REPORT_VIEWS.map((v) => (
          <button
            key={v.id}
            onClick={() => setView(v.id)}
            className={`font-disp text-btn font-medium px-4 py-1.5 rounded-[7px] transition-colors ${
              view === v.id ? 'bg-card-2 text-ink shadow-card' : 'text-muted hover:text-ink-2'
            }`}
          >
            {v.label}
          </button>
        ))}
      </div>
      {view === 'dayend' ? <DayEnd /> : <Journal />}
    </div>
  );
}

/* ---------------------------------------------------------------- Strategies */

export function Strategies({ s, onRefresh }) {
  const algo = s?.algo || {};
  const [strategies, setStrategies] = useState([]);
  const [activeStrats, setActiveStrats] = useState(algo.active_strategies || []);
  const [autoExec, setAutoExec] = useState(Boolean(algo.auto_execute));
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');

  useEffect(() => {
    api.getStrategies().then((data) => {
      if (data?.strategies) {
        setStrategies(data.strategies);
        if (data.active_strategies) setActiveStrats(data.active_strategies);
        if (data.auto_execute != null) setAutoExec(Boolean(data.auto_execute));
      }
    }).catch(() => {});
  }, [algo.active_strategies, algo.auto_execute]);

  const onSave = async (newStrats, newAuto) => {
    setSaving(true);
    try {
      await api.setActiveStrategy({
        strategies: newStrats,
        auto_execute: newAuto,
      });
      setActiveStrats(newStrats);
      setAutoExec(newAuto);
      setToast(`Active algorithms updated!`);
      setTimeout(() => setToast(''), 3000);
      if (onRefresh) onRefresh();
    } catch (e) {
      alert(e.message);
    } finally {
      setSaving(false);
    }
  };

  const toggleStrategy = (stratName) => {
    const newStrats = activeStrats.includes(stratName)
      ? activeStrats.filter(n => n !== stratName)
      : [...activeStrats, stratName];
    onSave(newStrats, autoExec);
  };

  return (
    <div className="space-y-cardgap">
      {toast && <Banner tone="green">{toast}</Banner>}

      <SectionHeader
        title="Algo strategies"
        sub={`${activeStrats.length} active · ${strategies.length} registered`}
        right={
          <span className={`chip font-semibold ${autoExec ? '!bg-ai-soft !text-ai !border-ai/40' : ''}`}>
            {autoExec ? 'AUTO-PILOT' : 'ASSISTED'}
          </span>
        }
      />

      {/* Active Strategy & Operational Mode Card */}
      <Card tone={autoExec ? 'ai' : ''}>
        <Eyebrow>Active execution posture</Eyebrow>

        <div className="font-disp text-contract font-semibold text-ink mt-2 leading-snug">
          {activeStrats.length > 0
            ? activeStrats.map(strat => strategies.find((x) => x.name === strat)?.display_name || strat.replace(/_/g, ' ').toUpperCase()).join(' · ')
            : 'NONE ACTIVE'}
        </div>
        <p className="text-sec text-ink-2 mt-2 leading-relaxed">
          {autoExec
            ? 'Signals meeting institutional criteria execute automatically through the Risk Engine FSM, straight to the broker.'
            : 'Signals require 1-tap manual review in the Signals tab before orders reach the broker.'}
        </p>

        <div className="mt-4 pt-3 border-t border-line">
          <Eyebrow className="mb-2">Switch execution mode</Eyebrow>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={saving}
              className={`rounded-block py-2.5 px-3 text-sec font-semibold border transition-all disabled:opacity-50 ${
                !autoExec
                  ? 'bg-ink text-paper border-ink'
                  : 'bg-card-2 text-ink-2 border-line hover:text-ink hover:border-line-strong'
              }`}
              onClick={() => onSave(activeStrats, false)}
            >
              ✋ Assisted
            </button>
            <button
              type="button"
              disabled={saving}
              className={`rounded-block py-2.5 px-3 text-sec font-semibold border transition-all disabled:opacity-50 ${
                autoExec
                  ? 'bg-ai text-white border-ai'
                  : 'bg-card-2 text-ink-2 border-line hover:text-ink hover:border-ai/50'
              }`}
              onClick={() => onSave(activeStrats, true)}
            >
              ⚡ Auto-Pilot
            </button>
          </div>
        </div>
      </Card>

      <Eyebrow className="!mt-1">Strategy catalog</Eyebrow>

      {/* Strategy Cards — each carries its own accent so the catalog reads as
          distinct entities rather than one undifferentiated stack. */}
      <div className="space-y-3">
        {strategies.map((st) => {
          const isActive = activeStrats.includes(st.name);
          const accent = STRATEGY_ACCENT[st.name] || 'ai';
          const A = ACCENT_CLASSES[accent];
          return (
            <Card
              key={st.name}
              hover
              className={`relative overflow-hidden ${isActive ? A.border : 'border-line'}`}
            >
              {/* accent spine */}
              <div className={`absolute left-0 inset-y-0 w-[3px] ${isActive ? A.bg : 'bg-line'}`} />

              <div className="flex items-start justify-between gap-2 pl-1.5">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${A.bg} ${isActive ? 'live-ring' : 'opacity-40'}`} />
                    <span className="font-disp font-semibold text-body text-ink">
                      {st.display_name || st.name.replace(/_/g, ' ').toUpperCase()}
                    </span>
                    <span className="chip">v{st.version || '1.0'}</span>
                    {st.execution_mode && st.execution_mode !== 'standard' && (
                      <span className={`chip ${A.chip}`}>{st.execution_mode.replace(/_/g, ' ')}</span>
                    )}
                  </div>
                  <div className="num text-eyebrow text-muted mt-1.5">
                    {(st.instrument_focus || ['NIFTY', 'SENSEX']).join(' · ')}
                  </div>
                </div>
                {isActive ? (
                  <button
                    className="chip !bg-green-soft !text-green !border-green/40 font-semibold cursor-pointer shrink-0"
                    disabled={saving}
                    onClick={() => toggleStrategy(st.name)}
                  >
                    ✓ ACTIVE
                  </button>
                ) : (
                  <button
                    className="btn-ghost !w-auto !py-1.5 px-3 text-sec shrink-0"
                    disabled={saving}
                    onClick={() => toggleStrategy(st.name)}
                  >
                    Activate
                  </button>
                )}
              </div>

              <p className="text-sec text-ink-2 mt-2.5 leading-relaxed">{st.description}</p>

              {st.default_params && (
                <div className="mt-3 pt-2.5 border-t border-line/60 flex flex-wrap gap-1.5 text-f11 num text-muted">
                  {Object.entries(st.default_params).map(([k, v]) => (
                    <span key={k} className="px-2 py-0.5 rounded-pill bg-line-soft text-ink-2">
                      {k}: <b>{String(v)}</b>
                    </span>
                  ))}
                </div>
              )}
            </Card>
          );
        })}
      </div>

      <ThunderboltOrderFlowCard active={activeStrats.includes('thunderbolt')} />
      <BreadthOrderFlowCard active={activeStrats.includes('breadth')} />
    </div>
  );
}

/* Reads {recorder_health, live_status, record} every 5s and explains, in
   plain terms, why Thunderbolt has or hasn't fired -- the per-poll-cycle
   decision trace the backend now persists, not just the final outcome. */
function ThunderboltOrderFlowCard({ active }) {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      api.getThunderboltStatus()
        .then((d) => { if (!cancelled) { setStatus(d); setErr(''); } })
        .catch((e) => { if (!cancelled) setErr(e.message); });
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (err) {
    return (
      <Card className="border border-line">
        <Eyebrow>Order Flow — Nifty Thunderbolt</Eyebrow>
        <p className="text-sec text-red mt-2">Could not load order-flow status: {err}</p>
      </Card>
    );
  }
  if (!status) {
    return (
      <Card className="border border-line">
        <Eyebrow>Order Flow — Nifty Thunderbolt</Eyebrow>
        <div className="mt-3 space-y-2">
          <Skeleton h={12} w="60%" />
          <Skeleton h={12} w="45%" />
        </div>
      </Card>
    );
  }

  const { recorder_health: health, live_status: live, record } = status;
  const trace = live?.trace;

  const recencySeconds = health?.last_update_at
    ? Math.round((Date.now() - new Date(health.last_update_at).getTime()) / 1000)
    : null;
  const feedLive = health?.connected || (recencySeconds !== null && recencySeconds < 15);

  const filterReasons = trace ? [
    trace.opposite_gate_skip && 'Opposite-side flow too strong — gated out',
    trace.pre_open_lock_skip && 'Pre-open extreme locked out this direction',
    trace.liquidity_reversal_flip && 'Early one-way liquidity reversed — direction flipped',
    trace.medium_regime_flip && 'MEDIUM regime fade detected — direction flipped',
    trace.over_stretch_skip && 'Reading already over-stretched — vetoed as a crescendo, not a start',
    trace.reversal_flip && 'Opposite extreme dominated the crossing — flipped as exhaustion',
  ].filter(Boolean) : [];

  const verdictReason = () => {
    if (record?.position) return `Position open (${record.position.direction}).`;
    if (record?.skipped) return `Day skipped: ${record.skip_reason || 'no reason recorded'}.`;
    if (!live) return 'No live status yet today — the recorder/paper-trader script may not be running.';
    if (!live.in_signal_window) return 'Outside today\'s signal window — not evaluating right now.';
    if (!trace) return 'Waiting for the first evaluation cycle.';
    if (filterReasons.length) return filterReasons.join(' · ');
    if (!trace.trigger) return `No qualifying crossing/breakout yet in the ${trace.regime} regime.`;
    return `${trace.final} — trigger confirmed, no filter blocked it.`;
  };

  return (
    <Card hover tone={active ? 'violet' : ''} className={active ? 'border-violet/50' : ''}>
      <div className="flex items-center justify-between">
        <Eyebrow>Order Flow — Nifty Thunderbolt</Eyebrow>
        <span className={`chip text-f11 font-semibold ${feedLive ? 'bg-green-soft text-green border-green' : 'bg-red-soft text-red border-red'}`}>
          {feedLive ? '● Feed live' : '○ Feed down'}
        </span>
      </div>
      {!active && (
        <p className="text-f11 text-muted mt-1">Not in your active strategies list — shown for visibility only.</p>
      )}

      <div className="grid grid-cols-2 gap-2 mt-3 text-f11 num">
        <div className="px-2.5 py-2 rounded-block bg-line-soft">
          <div className="text-muted">Last tick</div>
          <div className="text-ink font-semibold">{recencySeconds === null ? '—' : `${recencySeconds}s ago`}</div>
        </div>
        <div className="px-2.5 py-2 rounded-block bg-line-soft">
          <div className="text-muted">Reconnects today</div>
          <div className="text-ink font-semibold">{health?.reconnect_count ?? '—'}</div>
        </div>
        <div className="px-2.5 py-2 rounded-block bg-line-soft">
          <div className="text-muted">Prior-session VIX</div>
          <div className="text-ink font-semibold">
            {live?.prior_session_vix ?? '—'}
            {live?.vix_skip_at_or_above != null && (
              <span className="text-muted font-normal"> / skip ≥ {live.vix_skip_at_or_above}</span>
            )}
          </div>
        </div>
        <div className="px-2.5 py-2 rounded-block bg-line-soft">
          <div className="text-muted">Regime</div>
          <div className="text-ink font-semibold">
            {trace?.regime ?? '—'}
            {live?.recent_realized_vols?.length > 0 && (
              <span className="text-muted font-normal"> (avg {(
                live.recent_realized_vols.reduce((a, b) => a + b, 0) / live.recent_realized_vols.length
              ).toFixed(2)})</span>
            )}
          </div>
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-line/60">
        <div className="text-eyebrow num text-muted mb-1">Latest imbalance reading</div>
        <div className="font-disp text-lg font-bold text-ink">
          {live?.latest_imbalance_reading ?? '—'}
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-line/60">
        <div className="text-eyebrow num text-muted mb-1">Why it {record?.position ? 'fired' : 'hasn\'t fired'}</div>
        <p className="text-sec text-ink leading-relaxed">{verdictReason()}</p>
        {trace?.trigger && (
          <p className="text-f11 text-muted mt-1 num">
            Trigger: {trace.trigger.direction} via {trace.trigger.source} at {trace.trigger.value}
          </p>
        )}
      </div>
    </Card>
  );
}

/* Reads {recorder_health, live_status, record} every 5s for the
   cross-sectional breadth signal -- same pattern as ThunderboltOrderFlowCard,
   adapted for breadth's multi-connection health (3 recorders: 2 equity
   batches + 1 option-chain batch) and its own live_status shape. */
function BreadthOrderFlowCard({ active }) {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      api.getBreadthStatus()
        .then((d) => { if (!cancelled) { setStatus(d); setErr(''); } })
        .catch((e) => { if (!cancelled) setErr(e.message); });
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (err) {
    return (
      <Card className="border border-line">
        <Eyebrow>Order Flow — Breadth (NIFTY100)</Eyebrow>
        <p className="text-sec text-red mt-2">Could not load order-flow status: {err}</p>
      </Card>
    );
  }
  if (!status) {
    return (
      <Card className="border border-line">
        <Eyebrow>Order Flow — Breadth (NIFTY100)</Eyebrow>
        <div className="mt-3 space-y-2">
          <Skeleton h={12} w="60%" />
          <Skeleton h={12} w="45%" />
        </div>
      </Card>
    );
  }

  const { recorder_health: health, live_status: live, record } = status;
  const connections = ['equities_conn0', 'equities_conn1', 'options_conn0'];
  const now = Date.now();
  const connStatuses = connections.map((label) => {
    const h = health?.[label];
    const recencySeconds = h?.last_update_at ? Math.round((now - new Date(h.last_update_at).getTime()) / 1000) : null;
    const live_ = h?.connected || (recencySeconds !== null && recencySeconds < 15);
    return { label, live: live_, recencySeconds, nInstruments: h?.n_instruments };
  });
  const allLive = connStatuses.every((c) => c.live);

  const verdictReason = () => {
    if (record?.position) return `Position open (${record.position.direction}).`;
    if (record?.skipped) return `Day skipped: ${record.skip_reason || 'no reason recorded'}.`;
    if (!live) return 'No live status yet today — the recorder/paper-trader script may not be running.';
    if (live.n_stocks_reporting < (live.min_stocks_reporting ?? 20)) {
      return `Only ${live.n_stocks_reporting} stocks reporting so far — waiting for enough coverage before evaluating.`;
    }
    return `Mean breadth ${live.mean_breadth?.toFixed?.(3) ?? live.mean_breadth} — no qualifying crossing past ±${live.theta_cross} yet.`;
  };

  return (
    <Card hover tone={active ? 'cyan' : ''} className={active ? 'border-cyan/50' : ''}>
      <div className="flex items-center justify-between">
        <Eyebrow>Order Flow — Breadth (NIFTY100)</Eyebrow>
        <span className={`chip text-f11 font-semibold ${allLive ? 'bg-green-soft text-green border-green' : 'bg-red-soft text-red border-red'}`}>
          {allLive ? '● All feeds live' : '○ Feed(s) down'}
        </span>
      </div>
      {!active && (
        <p className="text-f11 text-muted mt-1">Not in your active strategies list — shown for visibility only.</p>
      )}

      <div className="grid grid-cols-3 gap-2 mt-3 text-f11 num">
        {connStatuses.map((c) => (
          <div key={c.label} className="px-2 py-2 rounded-block bg-line-soft">
            <div className="text-muted truncate">{c.label.replace('_conn', ' #')}</div>
            <div className={`font-semibold ${c.live ? 'text-green' : 'text-red'}`}>
              {c.recencySeconds === null ? 'no data' : `${c.recencySeconds}s ago`}
            </div>
            <div className="text-muted">{c.nInstruments ?? '—'} instr.</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-2 mt-2 text-f11 num">
        <div className="px-2.5 py-2 rounded-block bg-line-soft">
          <div className="text-muted">Stocks reporting</div>
          <div className="text-ink font-semibold">{live?.n_stocks_reporting ?? '—'} / 100</div>
        </div>
        <div className="px-2.5 py-2 rounded-block bg-line-soft">
          <div className="text-muted">Crossing threshold</div>
          <div className="text-ink font-semibold">±{live?.theta_cross ?? '—'}</div>
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-line/60">
        <div className="text-eyebrow num text-muted mb-1">Mean breadth reading</div>
        <div className="font-disp text-lg font-bold text-ink">
          {live?.mean_breadth != null ? live.mean_breadth.toFixed(4) : '—'}
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-line/60">
        <div className="text-eyebrow num text-muted mb-1">Why it {record?.position ? 'fired' : 'hasn\'t fired'}</div>
        <p className="text-sec text-ink leading-relaxed">{verdictReason()}</p>
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- Backtest */

function WiggleCurveVisualizer({ wiggleAnalysis }) {
  if (!wiggleAnalysis) return null;

  const points = wiggleAnalysis.curve_points || [];
  const maxPf = Math.max(...points.map((p) => Number(p.pf) || 0), 2.0);
  const baselinePf = Number(wiggleAnalysis.baseline_profit_factor ?? wiggleAnalysis.baseline_pf ?? 0);
  const minPf = Number(wiggleAnalysis.min_profit_factor ?? wiggleAnalysis.min_pf ?? 0);
  const maxPfVal = Number(wiggleAnalysis.max_profit_factor ?? wiggleAnalysis.max_pf ?? 0);
  const degradation = Number(wiggleAnalysis.degradation_pct ?? 0);
  const verdict = wiggleAnalysis.verdict || (degradation <= 25 ? 'PLATEAU' : 'NEEDLE');
  const isPlateau = verdict === 'PLATEAU';

  return (
    <Card className="border border-line">
      <div className="flex justify-between items-center">
        <div>
          <Eyebrow>Check 17 · Parameter Wiggle Sensitivity (±20%)</Eyebrow>
          <div className="text-sec text-ink-2 mt-0.5">
            Tests parameter plateau vs fragile overfitted needle.
          </div>
        </div>
        <span
          className={`chip font-bold text-xs ${
            isPlateau
              ? 'bg-green-soft text-green border-green'
              : 'bg-red-soft text-red border-red'
          }`}
        >
          {verdict}
        </span>
      </div>

      {/* Visual Bar Chart */}
      {points.length > 0 && (
        <div className="mt-4 pt-3 pb-1 bg-line-soft/40 rounded-block px-3 border border-line">
          <div className="flex items-center justify-between text-f11 text-muted num mb-3">
            <span className="font-semibold text-ink">Profit Factor Stability Curve</span>
            <span className="flex items-center gap-1.5 font-medium text-amber">
              <span className="w-2.5 h-0.5 bg-amber inline-block" />
              Check 14 Threshold (1.30 PF)
            </span>
          </div>

          <div className="relative h-28 flex items-end justify-between gap-2 px-2 pb-6">
            {/* 1.30 PF Guideline line */}
            <div
              className="absolute left-0 right-0 border-b border-dashed border-amber/70 pointer-events-none z-10 flex items-center justify-end pr-1"
              style={{ bottom: `${Math.min(Math.max((1.30 / maxPf) * 100, 10), 90)}%` }}
            >
              <span className="text-f9 num font-semibold text-amber bg-card/90 px-1 rounded shadow-xs">
                1.30
              </span>
            </div>

            {points.map((pt, idx) => {
              const pfVal = Number(pt.pf) || 0;
              const heightPct = Math.min(Math.max((pfVal / maxPf) * 100, 12), 100);
              const isBase = pt.label === 'BASELINE';
              const isPass = pfVal >= 1.3;

              return (
                <div key={idx} className="flex-1 flex flex-col items-center gap-1.5 h-full justify-end group relative">
                  <span className="num text-f11 font-semibold text-ink">
                    {pfVal.toFixed(2)}
                  </span>
                  <div className="w-full max-w-[42px] bg-line rounded-t-sm relative flex items-end h-full">
                    <div
                      className={`w-full rounded-t-sm transition-all duration-300 ${
                        isBase
                          ? isPass ? 'bg-ink' : 'bg-red'
                          : isPass ? 'bg-green' : pfVal >= 1.0 ? 'bg-amber' : 'bg-red'
                      }`}
                      style={{ height: `${heightPct}%` }}
                    />
                  </div>
                  <div className="text-center">
                    <span className={`block text-f10 num leading-tight font-medium ${isBase ? 'text-ink font-bold' : 'text-muted'}`}>
                      {pt.label}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-2 text-center text-xs num py-2.5 mt-3 bg-line-soft/60 rounded-block">
        <div>
          <span className="text-muted block text-f11">Baseline PF</span>
          <span className="font-bold text-ink">{baselinePf.toFixed(2)}</span>
        </div>
        <div>
          <span className="text-muted block text-f11">Wiggle Range</span>
          <span className="font-bold text-ink">[{minPf.toFixed(2)} – {maxPfVal.toFixed(2)}]</span>
        </div>
        <div>
          <span className="text-muted block text-f11">Degradation</span>
          <span className={`font-bold ${degradation <= 25 ? 'text-green' : 'text-red'}`}>
            {degradation.toFixed(1)}%
          </span>
        </div>
      </div>

      <p className="text-f11 text-muted mt-2 leading-relaxed">
        {isPlateau
          ? '✓ Broad parameter plateau confirmed. Edge does not evaporate when stop-loss or profit-target parameters shift ±20%.'
          : '⚠ Fragile peak / overfitted needle. Profit Factor collapses when parameters deviate ±20%. Strategy is vulnerable to regime change.'}
      </p>
    </Card>
  );
}

function GauntletChecklistExplorer({ checklist23 }) {
  const [activeSec, setActiveSec] = useState('ALL');
  const [expanded, setExpanded] = useState(false);

  if (!checklist23 || !checklist23.sections) return null;

  const sections = checklist23.sections;
  const filteredSections = activeSec === 'ALL'
    ? sections
    : sections.filter((s) => s.id === activeSec);

  const passesCount = checklist23.passes_count ?? 0;
  const totalCount = checklist23.total_count ?? 23;

  return (
    <Card className="border border-line">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Eyebrow>23-Point Gauntlet Checklist</Eyebrow>
            <span className="chip text-f10 font-bold bg-line-soft text-ink">
              {passesCount}/{totalCount} Passed
            </span>
          </div>
          <p className="text-sec text-muted mt-0.5">
            Institutional criteria across Design, Honesty, Validation, Risk &amp; Live.
          </p>
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-xs num font-semibold text-ai hover:underline"
        >
          {expanded ? 'Collapse' : 'Expand All'}
        </button>
      </div>

      {/* Section Filter Pills */}
      <div className="flex gap-1.5 overflow-x-auto py-2.5 mt-2 border-b border-line text-xs num no-scrollbar">
        <button
          onClick={() => setActiveSec('ALL')}
          className={`px-2.5 py-1 rounded-pill whitespace-nowrap transition-colors ${
            activeSec === 'ALL'
              ? 'bg-ink text-paper font-semibold'
              : 'bg-line-soft text-ink-2 hover:bg-line'
          }`}
        >
          All (23)
        </button>
        {sections.map((sec) => (
          <button
            key={sec.id}
            onClick={() => setActiveSec(sec.id)}
            className={`px-2.5 py-1 rounded-pill whitespace-nowrap transition-colors ${
              activeSec === sec.id
                ? 'bg-ink text-paper font-semibold'
                : 'bg-line-soft text-ink-2 hover:bg-line'
            }`}
          >
            Sec {sec.id} ({sec.checks.length})
          </button>
        ))}
      </div>

      {/* Checklist items */}
      <div className="mt-3 space-y-3">
        {filteredSections.map((sec) => (
          <div key={sec.id} className="space-y-1.5">
            <div className="text-f11 font-bold uppercase tracking-wider text-muted px-1 flex items-center justify-between">
              <span>{sec.title}</span>
              <span className="num text-f10">
                {sec.checks.filter((c) => c.passed).length}/{sec.checks.length} Pass
              </span>
            </div>

            <div className="space-y-1.5">
              {sec.checks.map((chk) => (
                <div
                  key={chk.num}
                  className={`p-2.5 rounded-block border transition-colors ${
                    chk.passed
                      ? 'bg-card border-line hover:border-line-soft'
                      : 'bg-amber-soft/30 border-amber/40'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="num text-f11 font-bold text-muted w-6">
                        #{chk.num < 10 ? `0${chk.num}` : chk.num}
                      </span>
                      <span className="text-xs font-semibold text-ink">
                        {chk.name}
                      </span>
                    </div>
                    <span
                      className={`chip text-f10 font-bold ${
                        chk.passed
                          ? 'bg-green-soft text-green border-green'
                          : 'bg-amber-soft text-amber border-amber'
                      }`}
                    >
                      {chk.passed ? '✓ PASS' : '✗ FAIL'}
                    </span>
                  </div>

                  {(expanded || !chk.passed) && (
                    <div className="mt-1.5 pl-8 text-f11 text-ink-2 leading-relaxed bg-line-soft/30 py-1 px-2 rounded">
                      {chk.detail}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* Groups leg-level trade rows into one position per entry/exit event —
   a 2-4 leg credit spread must read as ONE trade, not several. Falls back
   to grouping by `id` for any cached result computed before `position_id`
   existed. */
function groupTradesByPosition(trades) {
  const order = [];
  const byKey = new Map();
  for (const t of trades) {
    const key = t.position_id || t.id;
    if (!byKey.has(key)) {
      byKey.set(key, []);
      order.push(key);
    }
    byKey.get(key).push(t);
  }
  return order.map((key) => {
    const legs = byKey.get(key);
    const net_pnl = legs.reduce((s, l) => s + (l.net_pnl || 0), 0);
    const capital_used = legs.reduce((s, l) => s + (l.capital_used || 0), 0);
    return { key, legs, net_pnl, capital_used, first: legs[0] };
  });
}

function TradeReplayLog({ trades = [] }) {
  const [filter, setFilter] = useState('ALL');

  if (!trades || trades.length === 0) return null;

  const positions = groupTradesByPosition(trades);
  const filteredPositions = filter === 'ALL'
    ? positions
    : filter === 'WINS'
    ? positions.filter((p) => p.net_pnl > 0)
    : positions.filter((p) => p.net_pnl <= 0);

  const winsCount = positions.filter((p) => p.net_pnl > 0).length;
  const lossesCount = positions.filter((p) => p.net_pnl <= 0).length;

  return (
    <Card>
      <div className="flex items-center justify-between">
        <Eyebrow>Trade Replay Log ({positions.length} trades{trades.length !== positions.length ? `, ${trades.length} legs` : ''})</Eyebrow>
        <div className="flex gap-1 text-f11 num">
          <button
            onClick={() => setFilter('ALL')}
            className={`px-2 py-0.5 rounded-pill ${filter === 'ALL' ? 'bg-ink text-paper' : 'bg-line-soft text-ink-2'}`}
          >
            All ({positions.length})
          </button>
          <button
            onClick={() => setFilter('WINS')}
            className={`px-2 py-0.5 rounded-pill ${filter === 'WINS' ? 'bg-green text-paper' : 'bg-line-soft text-ink-2'}`}
          >
            Wins ({winsCount})
          </button>
          <button
            onClick={() => setFilter('LOSSES')}
            className={`px-2 py-0.5 rounded-pill ${filter === 'LOSSES' ? 'bg-red text-paper' : 'bg-line-soft text-ink-2'}`}
          >
            Losses ({lossesCount})
          </button>
        </div>
      </div>

      <div className="mt-3 space-y-2.5 max-h-96 overflow-y-auto pr-1">
        {filteredPositions.map((pos, idx) => {
          const legs = pos.legs;
          const isMultiLeg = legs.length > 1;
          return (
            <div key={pos.key} className="p-3 rounded-block bg-line-soft/40 border border-line text-xs num space-y-2">
              <div className="flex justify-between items-center">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-ink">#{idx + 1}</span>
                  {isMultiLeg && (
                    <span className="chip text-f9 font-semibold px-1.5 py-0.5 bg-line-soft text-ink-2 border-line">
                      {legs.length} legs
                    </span>
                  )}
                  <span
                    className={`chip text-f9 font-semibold py-0.5 ${
                      (pos.first.exit_reason || '').includes('Target')
                        ? 'bg-green-soft text-green border-green'
                        : (pos.first.exit_reason || '').includes('Stop')
                        ? 'bg-red-soft text-red border-red'
                        : 'bg-line-soft text-ink-2 border-line'
                    }`}
                  >
                    {pos.first.exit_reason || (pos.net_pnl > 0 ? 'TARGET_HIT' : 'STOP_LOSS_HIT')}
                  </span>
                </div>
                <span className={`font-bold text-sm ${pnlColor(pos.net_pnl)}`}>
                  {rupee(pos.net_pnl, true)}
                </span>
              </div>
              {pos.capital_used > 0 && (
                <span className="chip text-f9 font-semibold bg-ai-soft text-ai border-ai px-1.5 py-0.5">
                  Capital: {rupee(pos.capital_used)}
                </span>
              )}
              <div className="space-y-1.5">
                {legs.map((t, legIdx) => (
                  <LegRow key={legIdx} t={t} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function LegRow({ t }) {
  const entryP = Number(t.entry_price || 0);
  const exitP = Number(t.exit_price || 0);
  const ptsDiff = exitP - entryP;
  const [openDate, openTime] = (t.opened_at || '').split(' ');
  const [, closeTime] = (t.closed_at || '').split(' ');

  return (
    <div className="text-xs num space-y-1 border-t border-line/60 pt-1.5 first:border-t-0 first:pt-0">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`chip text-f10 font-bold px-1.5 py-0.5 ${
              t.direction === 'PE'
                ? 'bg-amber-soft text-amber border-amber'
                : 'bg-ai-soft text-ai border-ai'
            }`}
          >
            {t.direction}
          </span>
          {t.symbol && (
            <span className="font-semibold text-ink-2 text-f11">
              {t.symbol}
              {t.expiry && <span className="text-muted font-normal"> · exp {t.expiry}</span>}
            </span>
          )}
        </div>
        <span className={`font-semibold text-f11 ${pnlColor(t.net_pnl)}`}>
          {rupee(t.net_pnl, true)}
        </span>
      </div>

      <div className="flex justify-between items-center text-f11 text-muted">
        <span>
          {entryP.toFixed(2)} → {exitP.toFixed(2)}
          <span className={`ml-1.5 font-semibold ${ptsDiff >= 0 ? 'text-green' : 'text-red'}`}>
            ({ptsDiff >= 0 ? '+' : ''}{ptsDiff.toFixed(2)} pts)
          </span>
        </span>
      </div>

      <div className="flex justify-between text-f10 text-muted">
        <span>
          {openDate && openTime && closeTime
            ? `${openDate} · ${openTime} → ${closeTime} IST`
            : 'Intraday Bar Execution'}
        </span>
        <span>Slippage: {rupee(t.slippage_cost || 0)} · Fees: {rupee(t.costs || 0)} · Qty: {t.qty || 50}</span>
      </div>
    </div>
  );
}

export function Backtest({ liveJob } = {}) {
  const [inst, setInst] = useState('NIFTY');
  const [strat, setStrat] = useState('renko_strategy');
  const [days, setDays] = useState(5);
  const [timelineMode, setTimelineMode] = useState('preset');
  const [fromDate, setFromDate] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 10);
    return d.toISOString().split('T')[0];
  });
  const [toDate, setToDate] = useState(() => new Date().toISOString().split('T')[0]);
  const [wiggle, setWiggle] = useState(true);
  const [slippage, setSlippage] = useState(true);

  const [res, setRes] = useState(null);
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [history, setHistory] = useState([]);
  const [submitError, setSubmitError] = useState('');
  const jobIdRef = useRef(null);
  jobIdRef.current = jobId;

  const loading = job && (job.status === 'QUEUED' || job.status === 'RUNNING');

  const loadHistory = () => {
    api.listBacktestRuns(10).then((data) => setHistory(data.runs || [])).catch(() => {});
  };

  const applyProgress = (data) => {
    setJob(data);
    if (data.status === 'DONE') {
      if (data.result) {
        setRes(data.result);
      } else {
        api.getBacktestRun(jobIdRef.current).then((full) => full.result && setRes(full.result)).catch(() => {});
      }
      loadHistory();
    } else if (['FAILED', 'CANCELLED', 'ABORTED'].includes(data.status)) {
      loadHistory();
    }
  };

  // Recover an in-flight job across a page refresh — the sim keeps running on
  // the server's worker thread regardless of whether anyone is watching.
  useEffect(() => {
    api.listBacktestRuns(1).then((data) => {
      const latest = (data.runs || [])[0];
      if (latest && ['QUEUED', 'RUNNING'].includes(latest.status)) {
        setJobId(latest.id);
        setJob({ status: latest.status, phase: latest.status, pct: 0, sessions_done: 0, sessions_total: 0, session_date: '' });
      }
    }).catch(() => {});
    loadHistory();
  }, []);

  // Fast path: live progress pushed over the /live WebSocket.
  useEffect(() => {
    if (liveJob && liveJob.job_id === jobIdRef.current) {
      applyProgress(liveJob);
    }
  }, [liveJob]);

  // Poll fallback — also the only path if the socket is down or reconnecting.
  useEffect(() => {
    if (!jobId || !loading) return;
    const t = setInterval(() => {
      api.getBacktestRun(jobId).then(applyProgress).catch(() => {});
    }, 2000);
    return () => clearInterval(t);
  }, [jobId, loading]);

  const onRun = async () => {
    setSubmitError('');
    try {
      const payload = {
        instrument: inst,
        strategy: strat,
        wiggle_test: wiggle,
        enable_slippage: slippage,
      };
      if (timelineMode === 'custom' && fromDate && toDate) {
        payload.from_date = fromDate;
        payload.to_date = toDate;
      } else {
        payload.days = days;
      }
      const data = await api.submitBacktestRun(payload);
      setRes(null);
      setJobId(data.job_id);
      setJob({ status: 'QUEUED', phase: 'QUEUED', pct: 0, sessions_done: 0, sessions_total: 0, session_date: '' });
    } catch (e) {
      setSubmitError(e.message);
    }
  };

  const onCancel = () => {
    if (jobId) api.cancelBacktestRun(jobId).catch((e) => setSubmitError(e.message));
  };

  const onSelectHistoryRun = (row) => {
    if (!row.result) return;
    setRes(row.result);
    setJobId(row.id);
    setJob({ status: row.status, phase: row.status, pct: 100, sessions_done: 0, sessions_total: 0, session_date: '' });
  };

  const wiggleAnalysis = res?.wiggle_analysis || res?.wiggle_test;
  const checklist23 = res?.checklist_23;
  const sessionItems = Object.entries(res?.session_pnls || {}).map(([label, value]) => ({ label, value }));

  return (
    <div className="space-y-cardgap">
      <Card>
        <div className="flex items-center justify-between">
          <Eyebrow>23-Point Gauntlet Backtesting Lab</Eyebrow>
          <span className="chip text-f10 text-ai bg-ai-soft border-ai-soft">Check 01–23</span>
        </div>
        <p className="text-body text-ink-2 mt-1.5">
          Replays real 1-minute OHLCV + option-chain OI/premium candles (IEA archive, 2021–present; falls back to on-demand DhanHQ for anything the archive doesn't cover). Evaluates honest slippage (Check 09) and parameter sensitivity (Check 17: Plateau vs Needle).
        </p>

        <div className="mt-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <div>
              <label className="text-eyebrow num text-muted block mb-1">Index</label>
              <select
                className="w-full rounded-block px-3 py-2 border border-line bg-card text-ink num text-sec"
                value={inst}
                onChange={(e) => setInst(e.target.value)}
              >
                <option value="NIFTY">NIFTY</option>
                <option value="BANKNIFTY">BANKNIFTY</option>
                <option value="SENSEX">SENSEX</option>
                <option value="FINNIFTY">FINNIFTY</option>
              </select>
              {inst !== 'NIFTY' && (
                <p className="text-f10 text-amber mt-1 leading-snug">
                  Real archive covers NIFTY only — this index needs a live broker connection or will return NO_DATA.
                </p>
              )}
            </div>
            <div>
              <label className="text-eyebrow num text-muted block mb-1">Strategy</label>
              <select
                className="w-full rounded-block px-2 py-2 border border-line bg-card text-ink text-sec"
                value={strat}
                onChange={(e) => setStrat(e.target.value)}
              >
                <option value="renko_strategy">Dynamic Renko</option>
                <option value="weekly_credit_spread">Weekly Credit Spread (PCR)</option>
                <option value="thunderbolt">Nifty Thunderbolt (1x2 Backspread)</option>
              </select>
              {strat === 'thunderbolt' && (
                <p className="text-f10 text-amber mt-1 leading-snug">
                  Live order-flow signal — no historical order-book data exists, so this cannot be
                  backtested (running it will return a 0-trade result explaining why). Paper-trade
                  only, once the recorder is live during market hours.
                </p>
              )}
            </div>
            <div>
              <label className="text-eyebrow num text-muted block mb-1">Timeline</label>
              <select
                className="w-full rounded-block px-3 py-2 border border-line bg-card text-ink num text-sec"
                value={timelineMode === 'custom' ? 'custom' : days}
                onChange={(e) => {
                  if (e.target.value === 'custom') {
                    setTimelineMode('custom');
                  } else {
                    setTimelineMode('preset');
                    setDays(Number(e.target.value));
                  }
                }}
              >
                <option value={5}>Last 5 Days</option>
                <option value={10}>Last 10 Days</option>
                <option value={15}>Last 15 Days</option>
                <option value={20}>Last 20 Days</option>
                <option value={30}>Last 30 Days</option>
                <option value="custom">📅 Custom Range</option>
              </select>
            </div>
          </div>

          {timelineMode === 'custom' && (
            <div className="grid grid-cols-2 gap-2 p-2.5 rounded-block bg-line/20 border border-line">
              <div>
                <label className="text-f10 uppercase font-bold text-muted block mb-1">From Date</label>
                <input
                  type="date"
                  className="w-full rounded-block px-2.5 py-1.5 border border-line bg-card text-ink text-xs font-mono"
                  value={fromDate}
                  onChange={(e) => setFromDate(e.target.value)}
                />
              </div>
              <div>
                <label className="text-f10 uppercase font-bold text-muted block mb-1">To Date</label>
                <input
                  type="date"
                  className="w-full rounded-block px-2.5 py-1.5 border border-line bg-card text-ink text-xs font-mono"
                  value={toDate}
                  onChange={(e) => setToDate(e.target.value)}
                />
              </div>
            </div>
          )}

          <div className="space-y-1.5 pt-1">
            <label className="flex items-center gap-2 cursor-pointer text-xs text-ink-2 select-none">
              <input
                type="checkbox"
                checked={wiggle}
                onChange={(e) => setWiggle(e.target.checked)}
                className="accent-ink rounded"
              />
              <span>Check 17: Parameter Wiggle Test (±20% Plateau vs Needle)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer text-xs text-ink-2 select-none">
              <input
                type="checkbox"
                checked={slippage}
                onChange={(e) => setSlippage(e.target.checked)}
                className="accent-ink rounded"
              />
              <span>Check 09: Honest Market Slippage Model</span>
            </label>
          </div>

          <button className="btn bg-ink text-paper w-full" onClick={onRun} disabled={loading}>
            {loading ? 'Running 23-Point Gauntlet...' : 'Run 23-Point Gauntlet Backtest'}
          </button>
        </div>
      </Card>

      {submitError && <Banner tone="red">{submitError}</Banner>}

      {loading && <BacktestProgressCard job={job} onCancel={onCancel} />}

      {job?.status === 'CANCELLED' && <Banner tone="amber">Backtest cancelled.</Banner>}
      {job?.status === 'FAILED' && <Banner tone="red">Backtest failed: {job.error || 'unknown error'}</Banner>}
      {job?.status === 'ABORTED' && <Banner tone="amber">Backtest aborted — the server restarted mid-run. Try again.</Banner>}

      {res && (
        <div className="space-y-cardgap">
          {/* Gauntlet Scorecard Verdict Card -- meaningless for a strategy
              that never ran (live-only signal, no historical data), so a
              pass/fail checklist verdict here would just be confusing. */}
          {res.data_source !== 'LIVE_ORDER_FLOW_ONLY' && (
          <div
            className={`card p-4 border relative overflow-hidden ${
              res.gauntlet_tone === 'green' || res.passes_checklist
                ? 'bg-green-soft/50 border-green text-ink'
                : res.gauntlet_tone === 'amber'
                ? 'bg-amber-soft/50 border-amber text-ink'
                : 'bg-red-soft/50 border-red text-ink'
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-base font-bold">
                    {res.gauntlet_tone === 'green' || res.passes_checklist ? '✓' : '⚠'}
                  </span>
                  <div className="font-disp text-contract font-bold tracking-tight">
                    {res.gauntlet_verdict || (res.passes_checklist ? 'INCUBATE (MINIMUM SIZE)' : 'KILL: EDGE IS NOT REAL')}
                  </div>
                </div>
                <div className="text-sec text-ink-2 mt-1 leading-relaxed">
                  {res.gauntlet_verdict_desc ||
                    (res.passes_checklist
                      ? 'Passes the Returns 23-Point Gauntlet. Edge demonstrates honest positive expectancy after full friction, adverse stop slippage, and parameter stability.'
                      : 'Edge failed to pass the required checklist criteria. Review failing checks below before committing real capital.')}
                </div>
              </div>
              <div className="text-right shrink-0">
                <span className="chip font-bold text-xs bg-card border shadow-xs">
                  {res.checklist_23?.passes_count ?? (res.passes_checklist ? 23 : 19)}/23 PASSED
                </span>
                <div className="num text-f11 text-muted mt-1 font-semibold">
                  PF {res.profit_factor}
                </div>
              </div>
            </div>

            {/* Progress Bar */}
            <div className="mt-3 w-full bg-card/70 rounded-pill h-2 overflow-hidden border border-line">
              <div
                className={`h-full rounded-pill transition-all duration-500 ${
                  res.gauntlet_tone === 'green' || res.passes_checklist
                    ? 'bg-green'
                    : res.gauntlet_tone === 'amber'
                    ? 'bg-amber'
                    : 'bg-red'
                }`}
                style={{
                  width: `${Math.round(
                    ((res.checklist_23?.passes_count ?? (res.passes_checklist ? 23 : 19)) / 23) * 100
                  )}%`,
                }}
              />
            </div>

            <div className="mt-2.5 flex items-center justify-between text-f11 text-muted italic">
              <span>“7 ship. 993 die. The checklist is the executioner.”</span>
              <span className="num not-italic text-f10">Doc RW/INCUB/2026-08</span>
            </div>
          </div>
          )}

          {/* Data Provenance Badge */}
          <div className={`p-3 rounded-block border flex items-center justify-between text-xs num ${
            res.data_source?.includes('REAL') || res.data_source?.includes('CACHE') || res.data_source?.includes('STORE')
              ? 'bg-green-soft/40 border-green/30 text-ink'
              : 'bg-amber-soft/50 border-amber/40 text-amber'
          }`}>
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${
                res.data_source?.includes('REAL') || res.data_source?.includes('CACHE') || res.data_source?.includes('STORE')
                  ? 'bg-green'
                  : 'bg-amber'
              }`} />
              <span className="font-semibold">Data Provenance:</span>
              <span className="font-mono text-f11 px-1.5 py-0.5 rounded bg-card border border-line">
                {res.data_source || 'SYNTHETIC_MODEL'}
              </span>
            </div>
            <span className="text-muted text-f11">
              {res.bars_evaluated ? `${res.bars_evaluated.toLocaleString()} bars replay` : `${res.days} days`}
            </span>
          </div>

          {res.data_warning && (
            res.data_source?.includes('NO_DATA') ? (
              <div className="p-4 rounded-block bg-red-soft/70 border border-red/60 text-xs space-y-2">
                <div className="font-bold text-red flex items-center gap-2 text-sm">
                  <span>🔌</span> Broker Not Connected — Real Data Required
                </div>
                <p className="text-xs leading-relaxed text-ink">
                  {res.data_warning}
                </p>
                <div className="text-f11 text-muted">
                  Go to <strong>System</strong> → configure <code>DHAN_CLIENT_ID</code> and <code>DHAN_ACCESS_TOKEN</code> in your <code>.env</code>, then restart the server.
                </div>
              </div>
            ) : (
              <div className="p-3 rounded-block bg-amber-soft/60 border border-amber/40 text-xs text-amber space-y-1">
                <div className="font-bold flex items-center gap-1.5">
                  <span>⚠️</span> Edge Unverified on Real Market Data
                </div>
                <p className="text-f11 leading-relaxed text-ink-2">
                  {res.data_warning}
                </p>
              </div>
            )
          )}

          {/* Check 17 Wiggle Test Result */}
          {wiggleAnalysis && <WiggleCurveVisualizer wiggleAnalysis={wiggleAnalysis} />}

          {/* Performance Summary Card */}
          <Card>
            <Row
              left={<Eyebrow>{res.strategy || strat} · {res.instrument} ({res.days} Sessions)</Eyebrow>}
              right={
                <span className={`num text-contract font-bold ${pnlColor(res.final_pnl)}`}>
                  {rupee(res.final_pnl, true)}
                </span>
              }
            />
            <div className="text-f11 text-muted num mt-0.5">
              Starting capital: {rupee(res.initial_capital)}
            </div>

            <div className="grid grid-cols-3 gap-2 text-center text-xs num py-3 mt-3 bg-line-soft rounded-block">
              <div>
                <span className="text-muted block text-f11">Win %</span>
                <span className="font-semibold text-body">{res.win_pct}%</span>
              </div>
              <div>
                <span className="text-muted block text-f11">Trades</span>
                <span className="font-semibold text-body">{res.total_trades} ({res.wins}W/{res.losses}L)</span>
              </div>
              <div>
                <span className="text-muted block text-f11">Profit Factor</span>
                <span className={`font-semibold text-body ${res.profit_factor >= 1.3 ? 'text-green' : 'text-ink'}`}>
                  {res.profit_factor} <span className="text-f10 text-muted font-normal">(≥1.3 req)</span>
                </span>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-2 text-center text-xs num py-2 mt-2">
              <div>
                <span className="text-muted block text-f11">Gross P&amp;L</span>
                <span className={pnlColor(res.gross_pnl)}>{rupee(res.gross_pnl, true)}</span>
              </div>
              <div>
                <span className="text-muted block text-f11">Costs &amp; Slippage</span>
                <span className="text-muted">{rupee(res.total_costs)}</span>
              </div>
              <div>
                <span className="text-muted block text-f11">Max Drawdown</span>
                <span className="text-red">{rupee(res.max_drawdown)}</span>
              </div>
            </div>
          </Card>

          {/* Equity Curve */}
          {(res.trades || []).length > 0 && (
            <Card>
              <Row
                left={<Eyebrow>Equity Curve</Eyebrow>}
                right={<span className="num text-f11 text-muted">{res.trades.length} trades</span>}
              />
              <div className="mt-2">
                <EquityCurve trades={res.trades} />
              </div>
            </Card>
          )}

          {/* Session-by-Session Breakdown */}
          {sessionItems.length > 0 && (
            <Card>
              <Row
                left={<Eyebrow>Session-by-Session P&amp;L</Eyebrow>}
                right={<span className="num text-f11 text-muted">{sessionItems.length} sessions</span>}
              />
              <div className="mt-2">
                <MiniBars items={sessionItems} />
              </div>
            </Card>
          )}

          {/* Interactive 23-Point Gauntlet Checklist Explorer */}
          {checklist23 && <GauntletChecklistExplorer checklist23={checklist23} />}

          {/* Trade Replay Log */}
          {(res.trades || []).length > 0 && <TradeReplayLog trades={res.trades} />}
        </div>
      )}

      <RunHistory history={history} onSelect={onSelectHistoryRun} currentJobId={jobId} />
    </div>
  );
}

/* ---------------------------------------------------------------- Backtest sub-components (jobs/progress) */

function BacktestProgressCard({ job, onCancel }) {
  const pct = job?.pct || 0;
  const elapsed = job?.elapsed_s || 0;
  const done = job?.sessions_done || 0;
  const total = job?.sessions_total || 0;
  const eta = total > 0 && done > 0 ? Math.max(0, Math.round((elapsed / done) * (total - done))) : null;

  return (
    <Card className="space-y-3">
      <Row
        left={<Eyebrow>Running Backtest</Eyebrow>}
        right={<span className="chip text-f10 bg-ai-soft text-ai border-ai-soft">{job?.phase || job?.status}</span>}
      />
      <ProgressRail pct={pct} tone="ai" />
      <div className="flex items-center justify-between text-f11 num text-muted">
        <span>
          {total ? `Session ${done}/${total}` : 'Starting…'}
          {job?.session_date ? ` · ${job.session_date}` : ''}
        </span>
        <span>
          {elapsed.toFixed(1)}s elapsed{eta !== null ? ` · ~${eta}s left` : ''}
        </span>
      </div>
      <button className="btn bg-red-soft text-red w-full" onClick={onCancel}>
        Cancel
      </button>
    </Card>
  );
}

function RunHistory({ history, onSelect, currentJobId }) {
  if (!history || history.length === 0) return null;
  return (
    <Card>
      <Eyebrow>Run History</Eyebrow>
      <div className="mt-2 space-y-1.5">
        {history.map((row) => {
          const tone =
            row.status === 'DONE' ? (row.result?.passes_checklist ? 'text-green' : 'text-ink-2')
            : row.status === 'FAILED' ? 'text-red'
            : 'text-muted';
          const range = row.from_date ? `${row.from_date}${row.to_date ? `–${row.to_date}` : ''}` : `${row.days}d`;
          return (
            <button
              key={row.id}
              type="button"
              onClick={() => onSelect(row)}
              disabled={!row.result}
              className={`w-full flex items-center justify-between text-left px-2.5 py-2 rounded-block border transition-colors ${
                row.id === currentJobId ? 'border-ai/40 bg-ai-soft/20' : 'border-line'
              } ${row.result ? 'hover:bg-line-soft cursor-pointer' : 'opacity-60 cursor-default'}`}
            >
              <span className="text-f11 num">
                <span className="font-semibold text-ink">{row.strategy}</span>
                <span className="text-muted"> · {row.instrument} · {range}</span>
              </span>
              <span className={`text-f11 num font-semibold ${tone}`}>
                {row.status}{row.result ? ` · PF ${row.result.profit_factor}` : ''}
              </span>
            </button>
          );
        })}
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- DataScreen */

export function DataScreen({ s, onRefresh }) {
  const [inst, setInst] = useState('NIFTY');
  const [days, setDays] = useState(5);
  const [loadingCandles, setLoadingCandles] = useState(false);
  const [loadingChain, setLoadingChain] = useState(false);
  const [statusMsg, setStatusMsg] = useState('');
  const [manualToken, setManualToken] = useState('');
  const [updatingToken, setUpdatingToken] = useState(false);
  const [dataStatus, setDataStatus] = useState(null);
  const [strategies, setStrategies] = useState([]);
  const activeStrats = s?.algo?.active_strategies || [];
  const h = s?.health || {};

  useEffect(() => {
    api.getDataStatus().then(setDataStatus).catch(() => {});
    api.getStrategies().then((data) => {
      if (data?.strategies) setStrategies(data.strategies);
    }).catch(() => {});
  }, [s]);

  const onFetchCandles = async () => {
    setLoadingCandles(true);
    setStatusMsg('');
    try {
      const res = await api.fetchCandles({ instrument: inst, days });
      setStatusMsg(res.message);
      if (onRefresh) onRefresh();
    } catch (e) {
      alert(e.message);
    } finally {
      setLoadingCandles(false);
    }
  };

  const onRefreshChain = async () => {
    setLoadingChain(true);
    setStatusMsg('');
    try {
      const res = await api.refreshChain();
      setStatusMsg(res.message);
      if (onRefresh) onRefresh();
    } catch (e) {
      alert(e.message);
    } finally {
      setLoadingChain(false);
    }
  };

  return (
    <div className="space-y-cardgap">
      {statusMsg && <Banner tone="green">{statusMsg}</Banner>}

      {/* Historical 1-Minute Candle Data */}
      <Card>
        <Eyebrow>DhanHQ Historical 1-Minute Candle Streaming</Eyebrow>
        <p className="text-body text-ink-2 mt-1.5">
          Stream 1-minute historical candles on demand from DhanHQ API v2. Backtesting and strategy indicator engines fetch exchange bars on the fly without local file caches.
        </p>

        <div className="grid grid-cols-2 gap-2 mt-3">
          <select
            className="rounded-block px-3 py-2 border border-line bg-card text-ink num text-sec"
            value={inst}
            onChange={(e) => setInst(e.target.value)}
          >
            <option value="NIFTY">NIFTY</option>
            <option value="BANKNIFTY">BANKNIFTY</option>
            <option value="SENSEX">SENSEX</option>
            <option value="FINNIFTY">FINNIFTY</option>
          </select>
          <select
            className="rounded-block px-3 py-2 border border-line bg-card text-ink num text-sec"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            <option value={5}>5 Days (~1,875 bars)</option>
            <option value={10}>10 Days (~3,750 bars)</option>
            <option value={20}>20 Days (~7,500 bars)</option>
          </select>
        </div>

        <button
          className="btn bg-ink text-paper mt-3"
          onClick={onFetchCandles}
          disabled={loadingCandles}
        >
          {loadingCandles ? 'Fetching from Dhan...' : 'Test On-Demand Dhan 1m Query'}
        </button>
      </Card>

      {/* Option Chain Snapshots & Walls */}
      <Card>
        <Eyebrow>Option Chain Snapshots &amp; Open Interest</Eyebrow>
        <p className="text-body text-ink-2 mt-1.5">
          Native DhanHQ option chain with live Greeks (Delta, Theta, Gamma, Vega), IV, Call Wall ceiling, and Put Wall floor calculation.
        </p>

        <div className="mt-3 space-y-2">
          {Object.entries(h.chain_fresh || {}).map(([sym, fresh]) => (
            <Row
              key={sym}
              left={<span className="font-disp font-semibold text-sec">{sym}</span>}
              right={
                <span className={`chip text-f10 ${fresh ? 'bg-green-soft text-green border-green' : 'bg-amber-soft text-amber border-amber'}`}>
                  {fresh ? 'Snapshot Fresh' : 'Stale Snapshot'}
                </span>
              }
            />
          ))}
        </div>

        <button
          className="btn-ghost mt-3 w-full"
          onClick={onRefreshChain}
          disabled={loadingChain}
        >
          {loadingChain ? 'Taking Snapshots...' : 'Force Option Chain Snapshot Refresh'}
        </button>
      </Card>

      {/* Broker Session & 30-Day Token */}
      <Card>
        <div className="flex items-center justify-between">
          <Eyebrow>DhanHQ Session &amp; 30-Day Token</Eyebrow>
          <span className={`chip text-f10 font-semibold ${
            dataStatus?.broker_authenticated
              ? 'bg-green-soft text-green border-green'
              : 'bg-red-soft text-red border-red animate-pulse'
          }`}>
            {dataStatus?.broker_authenticated ? 'Session Active' : 'Session Expired / Invalid'}
          </span>
        </div>

        <p className="text-body text-ink-2 mt-1.5">
          DhanHQ API tokens are valid for 30 days. Generate your token from web.dhan.co → Profile → DhanHQ Trading APIs. No daily morning 6 AM login required.
        </p>

        {dataStatus?.broker_auth_error && (
          <div className="mt-2.5 p-2.5 rounded-block bg-amber-soft border border-amber text-xs text-amber space-y-0.5">
            <span className="font-bold">Last Broker Response:</span>
            <div className="font-mono text-f11 opacity-90">{dataStatus.broker_auth_error}</div>
          </div>
        )}

        <div className="mt-3 space-y-2">
          <label className="text-eyebrow num text-muted block">
            Paste Fresh DhanHQ Access Token (30-Day)
          </label>
          <div className="flex gap-2">
            <input
              type="text"
              placeholder="Paste DhanHQ Access Token..."
              value={manualToken}
              onChange={(e) => setManualToken(e.target.value)}
              className="flex-1 rounded-block px-3 py-2 border border-line bg-card text-ink font-mono text-xs"
            />
            <button
              className="btn bg-ink text-paper px-3 py-2 text-xs font-medium"
              disabled={!manualToken.trim() || updatingToken}
              onClick={async () => {
                setUpdatingToken(true);
                try {
                  const res = await api.updateDhanToken({ access_token: manualToken.trim() });
                  alert(`Token Updated Successfully! 🔑\n\nSnippet: ${res.token_snippet}\n${res.message}`);
                  setManualToken('');
                  const fresh = await api.getDataStatus();
                  setDataStatus(fresh);
                  if (onRefresh) onRefresh();
                } catch (e) {
                  alert(`Token update failed: ${e.message}`);
                } finally {
                  setUpdatingToken(false);
                }
              }}
            >
              {updatingToken ? 'Updating...' : 'Apply Token'}
            </button>
          </div>
        </div>
      </Card>

      {/* Connection & Feed Health */}
      <Card>
        <Eyebrow>Connection Health</Eyebrow>
        <div className="mt-3 space-y-2">
          <HealthRow
            label="Market data feed"
            ok={!h.feed_degraded}
            detail={`${h.feed_subscribed ?? 0} symbols · ${num(h.feed_age_s, 1)}s`}
          />
          <HealthRow
            label="Option chains"
            ok={Object.values(h.chain_fresh || {}).every(Boolean)}
            detail={Object.entries(h.chain_fresh || {})
              .map(([k, v]) => `${k} ${v ? 'ok' : 'stale'}`)
              .join(' · ')}
          />
          <HealthRow
            label="LLM Intelligence"
            ok={(s?.llm?.failures ?? 0) === 0}
            detail={`${s?.llm?.calls_today ?? 0}/${s?.llm?.cap ?? 10} calls`}
          />
        </div>
      </Card>

      {/* Strategy Dependencies */}
      <Card>
        <Eyebrow>Active Strategy Dependencies</Eyebrow>
        <div className="mt-3 space-y-4">
          {strategies.filter(st => activeStrats.includes(st.name)).map(st => (
            <div key={st.name} className="border-b border-line last:border-0 pb-3 last:pb-0">
              <div className="font-semibold text-xs text-ink mb-1.5">{st.display_name || st.name.replace(/_/g, ' ').toUpperCase()}</div>
              {st.dependency_health ? (
                <div className="flex flex-wrap gap-1.5 text-f11 num">
                  {Object.entries(st.dependency_health.symbols || {}).map(([sym, status]) => (
                    <span key={sym} className={`px-2 py-0.5 rounded-pill border ${
                      status === 'OK' ? 'bg-green-soft text-green border-green-soft' :
                      status === 'STALE' ? 'bg-amber-soft text-amber border-amber-soft' :
                      'bg-red-soft text-red border-red-soft'
                    }`}>
                      TICK: {sym} ({status})
                    </span>
                  ))}
                  {Object.entries(st.dependency_health.timeframes || {}).map(([tf, status]) => (
                    <span key={tf} className={`px-2 py-0.5 rounded-pill border ${
                      status === 'OK' ? 'bg-green-soft text-green border-green-soft' :
                      'bg-amber-soft text-amber border-amber-soft'
                    }`}>
                      CANDLE: {tf} ({status})
                    </span>
                  ))}
                </div>
              ) : (
                <div className="text-xs text-ink-2">No dependencies tracked.</div>
              )}
            </div>
          ))}
          {strategies.filter(st => activeStrats.includes(st.name)).length === 0 && (
            <div className="text-sm text-ink-2">No active strategies.</div>
          )}
        </div>
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- System */

export function System({ s, onKill, onEnablePush, pushState, onOpenConfig, onRefresh }) {
  const [typed, setTyped] = useState('');
  const on = s.kill_switch === 'ON';
  return (
    <div className="space-y-cardgap">
      {/* Master Switch Card */}
      <div className="card bg-line-soft text-ink border-line">
        <Eyebrow>
          <span className="text-white/60">Master switch</span>
        </Eyebrow>
        <div className="font-disp text-contract font-bold mt-2">
          System {on ? 'ENABLED' : 'DISABLED'}
        </div>
        <p className="text-sec text-white/70 mt-2">
          Off stops all suggestions and system orders. Risk-reducing exits still run.
        </p>
        {on ? (
          <button
            className="btn bg-red text-paper mt-4"
            onClick={() => onKill('OFF', { reason: 'from PWA' })}
          >
            Turn OFF
          </button>
        ) : (
          <div className="mt-4 space-y-2">
            <input
              className="num w-full rounded-block px-3 py-3 text-ink"
              placeholder='type ENABLE'
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            <button
              className="btn bg-green text-paper"
              disabled={typed.trim() !== 'ENABLE'}
              onClick={() => onKill('ON', { typed })}
            >
              Re-enable
            </button>
          </div>
        )}
      </div>

      {/* Operating Mode & Violations */}
      <Card>
        <Row
          left={<Eyebrow>Operating Mode</Eyebrow>}
          right={
            <span className={`chip ${s.mode === 'LIVE' ? 'bg-green text-paper border-green' : 'bg-line-soft text-ink border-line'}`}>
              {s.mode}
            </span>
          }
        />
        <div className="num text-sec text-muted mt-3">
          Session {s.fsm?.session_date} · violations today {s.violations_today ?? 0}
          {s.week_locked && <span className="text-red font-semibold"> · WEEK LOCKED</span>}
        </div>
        <p className="text-sec text-muted mt-3 pt-3 border-t border-line leading-relaxed">
          Mode is set at boot. Change{' '}
          <code className="num bg-well px-1 py-0.5 rounded text-f11">IS_LIVE</code> in{' '}
          <code className="num bg-well px-1 py-0.5 rounded text-f11">.env</code> and restart the
          backend to switch.
        </p>
      </Card>

      {/* Notifications */}
      <Card>
        <Eyebrow>Notifications</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          iOS requires this app to be installed to the home screen before push works.
        </p>
        <button className="btn-ghost mt-3" onClick={onEnablePush}>
          {pushState || 'Enable push'}
        </button>
      </Card>

    </div>
  );
}

function HealthRow({ label, ok, detail }) {
  return (
    <Row
      left={
        <span className="text-body">
          <span
            className="inline-block w-2 h-2 rounded-full mr-2"
            style={{ background: ok ? 'rgb(var(--c-green))' : 'rgb(var(--c-amber))' }}
          />
          {label}
        </span>
      }
      right={<span className="num text-sec text-muted">{detail}</span>}
    />
  );
}

/* ---------------------------------------------------------------- Day-end */

export function DayEnd() {
  const [r, setR] = useState(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    api.getDayEnd().then(setR).catch((e) => setErr(e.message));
  }, []);
  if (err) return <Card><Empty>{err}</Empty></Card>;
  if (!r) return <SkeletonCard rows={4} />;
  const v = r.verdict || {};
  return (
    <div className="space-y-cardgap">
      <Card>
        <Eyebrow>{r.date}</Eyebrow>
        <div className={`num text-hero mt-2 ${pnlColor(v.realized)}`}>
          {rupee(v.realized, true)}
        </div>
        <div className="num text-sec text-muted mt-2">
          {(v.state_path || []).join(' → ')}
        </div>
        <div className="grid grid-cols-3 gap-3 mt-4 pt-4 border-t border-line">
          <Stat label="Trades" value={v.trades} />
          <Stat label="Win %" value={`${num(v.win_pct, 0)}%`} />
          <Stat label="Costs" value={rupee(v.costs)} />
        </div>
      </Card>

      <ClosedTradesTable
        title={`Executed · ${r.date}`}
        rows={(r.replay || []).map((t, i) => ({
          id: i,
          closed_at: t.closed_at,
          symbol: t.symbol,
          entry: t.entry,
          exit: t.exit,
          reason: t.reason || t.origin,
          costs: t.costs,
          pnl: t.net,
        }))}
        empty="No trades executed."
      />

      <Card>
        <Eyebrow>Suggested vs taken</Eyebrow>
        <p className="text-body text-ink-2 mt-2">{r.suggested_vs_taken?.note}</p>
      </Card>

      <Card>
        <Eyebrow>Discipline</Eyebrow>
        <div className="num text-contract mt-2">{r.discipline?.score}%</div>
        {(r.discipline?.detail || []).map((d, i) => (
          <div key={i} className="num text-sec text-amber mt-2">
            {d.kind} — {d.detail}
          </div>
        ))}
      </Card>
    </div>
  );
}

function PremarketCard() {
  const [pm, setPm] = useState(null);
  const [open, setOpen] = useState(true);
  const [loading, setLoading] = useState(false);

  const loadReport = async () => {
    setLoading(true);
    try {
      const data = await api.getPremarketReport();
      setPm(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadReport();
  }, []);

  const biasColor =
    pm?.bias === 'BULLISH'
      ? 'bg-green text-paper border-green'
      : pm?.bias === 'BEARISH'
      ? 'bg-red text-paper border-red'
      : 'bg-ink text-paper border-ink';

  const confColor =
    pm?.confidence === 'HIGH'
      ? 'bg-green/10 text-green border-green/30'
      : pm?.confidence === 'LOW'
      ? 'bg-amber/10 text-amber border-amber/30'
      : 'bg-ink/10 text-ink border-ink/30';

  return (
    <Card>
      <div className="flex justify-between items-center cursor-pointer" onClick={() => setOpen(!open)}>
        <div>
          <Eyebrow>Pre-Market Intelligence</Eyebrow>
          {pm ? (
            <div className="font-disp font-semibold text-sec mt-1 flex items-center gap-2 flex-wrap">
              <span>{pm.opening_gap} ({pm.gap_points > 0 ? `+${pm.gap_points} pts` : `${pm.gap_points || 0} pts`})</span>
              <span className={`chip text-xs px-2 py-0.5 ${biasColor}`}>{pm.bias}</span>
              {pm.confidence && (
                <span className={`chip text-xs px-2 py-0.5 border ${confColor}`}>
                  {pm.confidence} CONFIDENCE
                </span>
              )}
            </div>
          ) : (
            <div className="num text-sec text-muted mt-1">Loading intelligence...</div>
          )}
        </div>
        <button className="text-muted text-lg">{open ? '▲' : '▼'}</button>
      </div>

      {open && pm && (
        <div className="mt-3 pt-3 border-t border-line space-y-3">
          <p className="text-body text-ink-2 leading-relaxed">{pm.summary}</p>

          <div className="grid grid-cols-2 gap-2 text-xs num py-2 bg-line/20 rounded-block">
            <div className="px-2">
              <span className="text-muted block">NIFTY Put / Call Wall</span>
              <span className="font-semibold">{pm.nifty_support} – {pm.nifty_resistance}</span>
            </div>
            <div className="px-2">
              <span className="text-muted block">BANKNIFTY Range</span>
              <span className="font-semibold">{pm.banknifty_support} – {pm.banknifty_resistance}</span>
            </div>
          </div>

          <div>
            <span className="text-xs text-muted block mb-1">Sectors &amp; Specific Macro Drivers:</span>
            <div className="space-y-1.5 mt-1">
              {(pm.sectors_to_watch || []).map((sec, i) => {
                const sName = typeof sec === 'string' ? sec : sec.sector;
                const sReason = typeof sec === 'string' ? 'Macro read-through' : sec.reason;
                return (
                  <div key={i} className="text-xs bg-line/20 p-2.5 rounded-block flex justify-between items-center gap-2">
                    <span className="font-bold text-ink">{sName}</span>
                    <span className="text-muted text-right text-xs">{sReason}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {(pm.data_flags || []).length > 0 && (
            <div className="p-2.5 rounded-block bg-amber/10 border border-amber/20 text-xs text-amber leading-relaxed">
              <strong>Data Caveats:</strong> {pm.data_flags.join(' · ')}
            </div>
          )}

          <div className="p-2.5 rounded-block bg-ai/10 border border-ai/20 text-xs text-ink leading-relaxed">
            <strong className="text-ai block mb-0.5">Pre-Market Risk Advisory (09:15–09:35 AM):</strong>
            {pm.actionable_advice}
          </div>

          <button
            className="btn bg-ink text-paper text-xs py-2.5 w-full flex items-center justify-center gap-1.5 font-bold"
            onClick={(e) => {
              e.stopPropagation();
              loadReport();
            }}
            disabled={loading}
          >
            {loading ? 'Analyzing Live Macro & Financial News...' : 'Run Morning Intelligence Now ⚡'}
          </button>
        </div>
      )}
    </Card>
  );
}

