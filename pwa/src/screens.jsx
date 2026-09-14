/* The seven screens. docs/design_spec.md §3. */
import React, { useEffect, useState } from 'react';
import {
  Banner, Card, DayRail, Empty, Eyebrow, OriginTag, Row, SlTrack, Stat, StateChip,
  TradeDots, num, pnlColor, rupee, InstitutionalPostureCard,
} from './components.jsx';
import * as api from './api.js';

/* IST wall-clock from an ISO timestamp — the phone may be anywhere. */
const hhmm = (iso) =>
  new Date(iso).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata',
  });

/* ---------------------------------------------------------------- Today */

export function Today({ s, onSquareOff }) {
  const fsm = s.fsm || {};
  const m = s.market || {};
  return (
    <div className="space-y-cardgap">
      <div className="grid grid-cols-3 gap-2">
        {['SENSEX', 'NIFTY', 'INDIAVIX'].map((k) => {
          const q = m[k] || {};
          const ch = q.change_pct;
          return (
            <div key={k} className="card !rounded-block !p-3">
              <Eyebrow>{k === 'INDIAVIX' ? 'INDIA VIX' : k}</Eyebrow>
              <div className="num text-body font-semibold mt-1">
                {q.ltp == null
                  ? '—'
                  : Number(q.ltp).toLocaleString('en-IN', {
                      maximumFractionDigits: k === 'INDIAVIX' ? 2 : 0,
                    })}
              </div>
              <div className={`num text-eyebrow mt-0.5 ${pnlColor(ch)}`}>
                {ch == null ? '—' : `${ch > 0 ? '+' : ''}${num(ch, 2)}%`}
              </div>
            </div>
          );
        })}
      </div>

      <PremarketCard />

      {s.institutional &&
        Object.entries(s.institutional).map(([sym, data]) => (
          <InstitutionalPostureCard
            key={sym}
            instData={data}
            instrument={sym}
            spot={m[sym]?.ltp}
          />
        ))}

      <Card>
        <Row
          left={<Eyebrow>Day P&amp;L</Eyebrow>}
          right={<StateChip state={fsm.state} floor={fsm.floor} />}
        />
        <div className={`num text-hero mt-1 ${pnlColor(fsm.day_pnl)}`}>
          {rupee(fsm.day_pnl, true)}
        </div>
        <div className="text-sec text-ink-2 mt-1.5">
          {Math.round(((fsm.day_pnl || 0) / (s.target || 1)) * 100)}% of target · floor at{' '}
          <b className="num">{rupee(fsm.floor, true)}</b>
        </div>
        <DayRail
          dayPnl={fsm.day_pnl || 0}
          floor={fsm.floor || 0}
          target={s.target || 2500}
          lossLimit={s.loss_limit || 1050}
        />
        <div className="grid grid-cols-3 mt-4 border-t border-line">
          <div className="pt-3.5 pr-2">
            <Eyebrow>Trades</Eyebrow>
            <div className="mt-2">
              <TradeDots taken={fsm.trades_taken || 0} cap={fsm.trade_cap || 3} />
            </div>
          </div>
          <div className="pt-3.5 px-3 border-l border-line">
            <Eyebrow>Risk / trade</Eyebrow>
            <div className="num text-contract font-semibold mt-1">
              {rupee(s.risk_per_trade)}
            </div>
          </div>
          <div className="pt-3.5 pl-3 border-l border-line">
            <Eyebrow>Entry window</Eyebrow>
            <div className="num text-contract font-semibold mt-1">
              {s.in_entry_window ? `→ ${s.entry_close || '15:00'}` : 'closed'}
            </div>
          </div>
        </div>
        <div className="num text-eyebrow text-muted mt-3 pt-3 border-t border-line-soft">
          realized {rupee(fsm.realized, true)} · to floor {rupee(s.floor_distance)} · charges{' '}
          {rupee(s.charges_today)}
          {s.open_charges_est > 0 && ` (+${rupee(s.open_charges_est)} to exit)`}
        </div>
      </Card>

      {s.expiry_today?.length > 0 && (
        <Banner tone="amber">
          EXPIRY DAY · {s.expiry_today.join(', ')} — entries close 14:30
        </Banner>
      )}

      <Card>
        <Eyebrow>Closed today</Eyebrow>
        {(s.trades || []).filter((t) => t.status === 'CLOSED').length === 0 ? (
          <Empty>No closed trades yet.</Empty>
        ) : (
          <div className="mt-3 space-y-3">
            {(s.trades || [])
              .filter((t) => t.status === 'CLOSED')
              .map((t, i) => (
                <Row
                  key={t.id}
                  className="pb-3 border-b border-line last:border-0"
                  left={
                    <>
                      <Eyebrow>
                        Trade {i + 1}
                        {t.closed_at ? ` · closed ${hhmm(t.closed_at)}` : ''}
                      </Eyebrow>
                      <div className="font-disp text-body font-semibold mt-1">{t.symbol}</div>
                      <div className="num text-eyebrow text-muted mt-1">
                        {num(t.entry)} → {num(t.exit)} · {t.reason} · charges{' '}
                        {rupee(t.costs)}
                      </div>
                    </>
                  }
                  right={
                    <>
                      <div className={`num text-body font-semibold ${pnlColor(t.pnl)}`}>
                        {rupee(t.pnl, true)}
                      </div>
                      <div className="mt-1.5">
                        <OriginTag origin={t.origin} />
                      </div>
                    </>
                  }
                />
              ))}
          </div>
        )}
      </Card>

      {(s.positions || []).length > 0 && (
        <button className="btn-danger" onClick={onSquareOff}>
          Square off everything
        </button>
      )}

      <StatusStrip s={s} />
    </div>
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
          className={`chip font-semibold text-[11px] px-2 py-0.5 rounded-full border ${
            isAuto ? 'bg-ai-soft text-ai border-ai-soft' : 'bg-line-soft text-ink-2 border-line'
          }`}
        >
          {isAuto ? '⚡ Auto-Pilot' : '✋ Assisted'}
        </span>
      </div>
      <div className="font-disp text-[15px] font-semibold text-ink">
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
  if (ps.length === 0) {
    return (
      <Card>
        <Empty>Flat. Nothing at risk.</Empty>
      </Card>
    );
  }
  const openTrades = (s.trades || []).filter((t) => t.status === 'OPEN');
  return (
    <div className="space-y-cardgap">
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
                <span className="shrink-0 w-[22px] h-[22px] rounded-full bg-amber-soft text-amber grid place-items-center text-[11px]">
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
          className="w-[104px] h-[104px] mx-auto mb-6 rounded-full grid place-items-center text-[38px]"
          style={{ background: green ? '#E6F6F0' : '#FDEBEC' }}
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
  if (!j) return <Card><Empty>Loading…</Empty></Card>;

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

/* ---------------------------------------------------------------- Strategies */

export function Strategies({ s, onRefresh }) {
  const algo = s?.algo || {};
  const [strategies, setStrategies] = useState([]);
  const [activeStrat, setActiveStrat] = useState(algo.active_strategy || 'institutional_breakout');
  const [autoExec, setAutoExec] = useState(Boolean(algo.auto_execute));
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');

  useEffect(() => {
    api.getStrategies().then((data) => {
      if (data?.strategies) {
        setStrategies(data.strategies);
        if (data.active_strategy) setActiveStrat(data.active_strategy);
        if (data.auto_execute != null) setAutoExec(Boolean(data.auto_execute));
      }
    }).catch(() => {});
  }, [algo.active_strategy, algo.auto_execute]);

  const onSave = async (newStrat, newAuto) => {
    setSaving(true);
    try {
      await api.setActiveStrategy({
        strategy: newStrat,
        auto_execute: newAuto,
      });
      setActiveStrat(newStrat);
      setAutoExec(newAuto);
      setToast(`Active algorithm updated to ${newStrat}!`);
      setTimeout(() => setToast(''), 3000);
      if (onRefresh) onRefresh();
    } catch (e) {
      alert(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-cardgap">
      {toast && <Banner tone="green">{toast}</Banner>}

      {/* Active Strategy & Operational Mode Card */}
      <Card className="border border-line">
        <div className="flex items-center justify-between">
          <Eyebrow>Active Execution Posture</Eyebrow>
          <span
            className={`chip font-semibold text-[11px] px-2.5 py-0.5 rounded-full border ${
              autoExec ? 'bg-ai-soft text-ai border-ai-soft' : 'bg-line-soft text-ink-2 border-line'
            }`}
          >
            {autoExec ? '⚡ Auto-Pilot' : '✋ Assisted'}
          </span>
        </div>

        <div className="font-disp text-lock font-bold text-ink mt-2">
          {strategies.find((x) => x.name === activeStrat)?.display_name || activeStrat.replace(/_/g, ' ').toUpperCase()}
        </div>
        <p className="text-sec text-ink-2 mt-1">
          {autoExec
            ? 'Signals meeting institutional criteria automatically execute through the deterministic Risk Engine FSM directly to the broker.'
            : 'Signals require 1-tap manual review in the Signals tab before orders reach the broker.'}
        </p>

        <div className="mt-4 pt-3 border-t border-line">
          <label className="text-eyebrow num text-muted block mb-1.5">Switch Execution Mode</label>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={saving}
              className={`rounded-block py-2.5 px-3 text-xs font-semibold border transition-all ${
                !autoExec
                  ? 'bg-ink text-paper border-ink shadow-sm'
                  : 'bg-white text-ink-2 border-line hover:border-ink'
              }`}
              onClick={() => onSave(activeStrat, false)}
            >
              ✋ Assisted Mode
            </button>
            <button
              type="button"
              disabled={saving}
              className={`rounded-block py-2.5 px-3 text-xs font-semibold border transition-all ${
                autoExec
                  ? 'bg-ai text-white border-ai shadow-sm'
                  : 'bg-white text-ink-2 border-line hover:border-ai'
              }`}
              onClick={() => onSave(activeStrat, true)}
            >
              ⚡ Auto-Pilot
            </button>
          </div>
        </div>
      </Card>

      {/* Strategy Catalog Header */}
      <Card>
        <Eyebrow>Dynamic Strategy Catalog</Eyebrow>
        <p className="text-body text-ink-2 mt-1">
          Select an institutional strategy below to activate it live across market hours.
        </p>
      </Card>

      {/* Strategy Cards */}
      <div className="space-y-3">
        {strategies.map((st) => {
          const isActive = st.name === activeStrat;
          return (
            <Card
              key={st.name}
              className={`border transition-all ${isActive ? 'border-ai ring-1 ring-ai/30 shadow-sm' : 'border-line'}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-disp font-semibold text-body text-ink">
                      {st.display_name || st.name.replace(/_/g, ' ').toUpperCase()}
                    </span>
                    <span className="chip text-[10px] py-0 px-1.5">v{st.version || '1.0'}</span>
                  </div>
                  <div className="num text-eyebrow text-muted mt-0.5">
                    Focus: {(st.instrument_focus || ['NIFTY', 'SENSEX']).join(' · ')}
                  </div>
                </div>
                {isActive ? (
                  <span className="chip bg-green-soft text-green border-green text-[11px] font-semibold">
                    ✓ Active
                  </span>
                ) : (
                  <button
                    className="btn-ghost !w-auto text-xs py-1 px-3 border border-line"
                    disabled={saving}
                    onClick={() => onSave(st.name, autoExec)}
                  >
                    Activate
                  </button>
                )}
              </div>

              <p className="text-sec text-ink-2 mt-2.5 leading-relaxed">{st.description}</p>

              {st.default_params && (
                <div className="mt-3 pt-2.5 border-t border-line/60 flex flex-wrap gap-1.5 text-[11px] num text-muted">
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
    </div>
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
          <div className="flex items-center justify-between text-[11px] text-muted num mb-3">
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
              <span className="text-[9px] num font-semibold text-amber bg-card/90 px-1 rounded shadow-xs">
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
                  <span className="num text-[11px] font-semibold text-ink">
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
                    <span className={`block text-[10px] num leading-tight font-medium ${isBase ? 'text-ink font-bold' : 'text-muted'}`}>
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
          <span className="text-muted block text-[11px]">Baseline PF</span>
          <span className="font-bold text-ink">{baselinePf.toFixed(2)}</span>
        </div>
        <div>
          <span className="text-muted block text-[11px]">Wiggle Range</span>
          <span className="font-bold text-ink">[{minPf.toFixed(2)} – {maxPfVal.toFixed(2)}]</span>
        </div>
        <div>
          <span className="text-muted block text-[11px]">Degradation</span>
          <span className={`font-bold ${degradation <= 25 ? 'text-green' : 'text-red'}`}>
            {degradation.toFixed(1)}%
          </span>
        </div>
      </div>

      <p className="text-[11px] text-muted mt-2 leading-relaxed">
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
            <span className="chip text-[10px] font-bold bg-line-soft text-ink">
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
            <div className="text-[11px] font-bold uppercase tracking-wider text-muted px-1 flex items-center justify-between">
              <span>{sec.title}</span>
              <span className="num text-[10px]">
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
                      <span className="num text-[11px] font-bold text-muted w-6">
                        #{chk.num < 10 ? `0${chk.num}` : chk.num}
                      </span>
                      <span className="text-xs font-semibold text-ink">
                        {chk.name}
                      </span>
                    </div>
                    <span
                      className={`chip text-[10px] font-bold ${
                        chk.passed
                          ? 'bg-green-soft text-green border-green'
                          : 'bg-amber-soft text-amber border-amber'
                      }`}
                    >
                      {chk.passed ? '✓ PASS' : '✗ FAIL'}
                    </span>
                  </div>

                  {(expanded || !chk.passed) && (
                    <div className="mt-1.5 pl-8 text-[11px] text-ink-2 leading-relaxed bg-line-soft/30 py-1 px-2 rounded">
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

function TradeReplayLog({ trades = [] }) {
  const [filter, setFilter] = useState('ALL');

  if (!trades || trades.length === 0) return null;

  const filtered = filter === 'ALL'
    ? trades
    : filter === 'WINS'
    ? trades.filter((t) => t.net_pnl > 0)
    : trades.filter((t) => t.net_pnl <= 0);

  const winsCount = trades.filter((t) => t.net_pnl > 0).length;
  const lossesCount = trades.filter((t) => t.net_pnl <= 0).length;

  return (
    <Card>
      <div className="flex items-center justify-between">
        <Eyebrow>Trade Replay Log ({trades.length} trades)</Eyebrow>
        <div className="flex gap-1 text-[11px] num">
          <button
            onClick={() => setFilter('ALL')}
            className={`px-2 py-0.5 rounded-pill ${filter === 'ALL' ? 'bg-ink text-paper' : 'bg-line-soft text-ink-2'}`}
          >
            All ({trades.length})
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
        {filtered.map((t, idx) => {
          const isWin = t.net_pnl > 0;
          const exitReason = t.exit_reason || (isWin ? 'TARGET_HIT' : 'STOP_LOSS_HIT');
          const entryP = Number(t.entry_price || 0);
          const exitP = Number(t.exit_price || 0);
          const ptsDiff = exitP - entryP;

          return (
            <div
              key={idx}
              className="p-3 rounded-block bg-line-soft/40 border border-line text-xs num space-y-1.5"
            >
              <div className="flex justify-between items-center">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-ink">
                    #{idx + 1}
                  </span>
                  <span
                    className={`chip text-[10px] font-bold px-1.5 py-0.5 ${
                      t.direction === 'CE'
                        ? 'bg-green-soft text-green border-green'
                        : 'bg-red-soft text-red border-red'
                    }`}
                  >
                    {t.direction}
                  </span>
                  <span className="font-semibold text-ink text-[12px]">
                    {t.symbol || `${t.instrument || 'NIFTY'}_${t.direction}`}
                  </span>
                </div>
                <span className={`font-bold text-sm ${pnlColor(t.net_pnl)}`}>
                  {rupee(t.net_pnl, true)}
                </span>
              </div>

              <div className="flex justify-between items-center text-[11px] text-muted">
                <div className="flex items-center gap-1.5">
                  <span>
                    {entryP.toFixed(2)} → {exitP.toFixed(2)}
                  </span>
                  <span className={ptsDiff >= 0 ? 'text-green font-semibold' : 'text-red font-semibold'}>
                    ({ptsDiff >= 0 ? '+' : ''}{ptsDiff.toFixed(2)} pts)
                  </span>
                </div>
                <span
                  className={`chip text-[9px] font-semibold py-0.5 ${
                    exitReason.includes('TARGET')
                      ? 'bg-green-soft text-green border-green'
                      : exitReason.includes('STOP')
                      ? 'bg-red-soft text-red border-red'
                      : 'bg-line-soft text-ink-2 border-line'
                  }`}
                >
                  {exitReason}
                </span>
              </div>

              <div className="flex justify-between text-[10px] text-muted pt-1 border-t border-line/60">
                <span>
                  {t.opened_at && t.closed_at ? `${t.opened_at} → ${t.closed_at} IST` : 'Intraday Bar Execution'}
                </span>
                <span>
                  Slippage: {rupee(t.slippage_cost || 0)} · Fees: {rupee(t.costs || 0)} · Qty: {t.qty || 65}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export function Backtest() {
  const [inst, setInst] = useState('NIFTY');
  const [strat, setStrat] = useState('institutional_breakout');
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
  const [loading, setLoading] = useState(false);

  const onRun = async () => {
    setLoading(true);
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
      const data = await api.runStrategyBacktest(payload);
      setRes(data.result || data);
    } catch (e) {
      alert(e.message);
    } finally {
      setLoading(false);
    }
  };

  const wiggleAnalysis = res?.wiggle_analysis || res?.wiggle_test;
  const checklist23 = res?.checklist_23;

  return (
    <div className="space-y-cardgap">
      <Card>
        <div className="flex items-center justify-between">
          <Eyebrow>23-Point Gauntlet Backtesting Lab</Eyebrow>
          <span className="chip text-[10px] text-ai bg-ai-soft border-ai-soft">Check 01–23</span>
        </div>
        <p className="text-body text-ink-2 mt-1.5">
          Replay 1-minute OHLCV candles through dynamic algorithms via on-demand DhanHQ Data API. Evaluates honest slippage (Check 09) and parameter sensitivity (Check 17: Plateau vs Needle).
        </p>

        <div className="mt-4 space-y-3">
          <div className="grid grid-cols-3 gap-2">
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
            </div>
            <div>
              <label className="text-eyebrow num text-muted block mb-1">Strategy</label>
              <select
                className="w-full rounded-block px-2 py-2 border border-line bg-card text-ink text-sec"
                value={strat}
                onChange={(e) => setStrat(e.target.value)}
              >
                <option value="institutional_breakout">Breakout</option>
                <option value="option_wall_squeeze">Wall Squeeze</option>
                <option value="wall_mean_reversion">Mean Rev</option>
              </select>
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
                <label className="text-[10px] uppercase font-bold text-muted block mb-1">From Date</label>
                <input
                  type="date"
                  className="w-full rounded-block px-2.5 py-1.5 border border-line bg-card text-ink text-xs font-mono"
                  value={fromDate}
                  onChange={(e) => setFromDate(e.target.value)}
                />
              </div>
              <div>
                <label className="text-[10px] uppercase font-bold text-muted block mb-1">To Date</label>
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

      {res && (
        <div className="space-y-cardgap">
          {/* Gauntlet Scorecard Verdict Card */}
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
                <div className="num text-[11px] text-muted mt-1 font-semibold">
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

            <div className="mt-2.5 flex items-center justify-between text-[11px] text-muted italic">
              <span>“7 ship. 993 die. The checklist is the executioner.”</span>
              <span className="num not-italic text-[10px]">Doc RW/INCUB/2026-08</span>
            </div>
          </div>

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
              <span className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-card border border-line">
                {res.data_source || 'SYNTHETIC_MODEL'}
              </span>
            </div>
            <span className="text-muted text-[11px]">
              {res.bars_evaluated ? `${res.bars_evaluated.toLocaleString()} bars replay` : `${res.days} days`}
            </span>
          </div>

          {res.data_warning && (
            <div className="p-3 rounded-block bg-amber-soft/60 border border-amber/40 text-xs text-amber space-y-1">
              <div className="font-bold flex items-center gap-1.5">
                <span>⚠️</span> Edge Unverified on Real Market Data
              </div>
              <p className="text-[11px] leading-relaxed text-ink-2">
                {res.data_warning}
              </p>
            </div>
          )}

          {/* Check 17 Wiggle Test Result */}
          {wiggleAnalysis && <WiggleCurveVisualizer wiggleAnalysis={wiggleAnalysis} />}

          {/* Performance Summary Card */}
          <Card>
            <Row
              left={<Eyebrow>{res.strategy || strat} · {res.instrument} ({res.days} Days)</Eyebrow>}
              right={
                <span className={`num text-contract font-bold ${pnlColor(res.final_pnl)}`}>
                  {rupee(res.final_pnl, true)}
                </span>
              }
            />

            <div className="grid grid-cols-3 gap-2 text-center text-xs num py-3 mt-3 bg-line-soft rounded-block">
              <div>
                <span className="text-muted block text-[11px]">Win %</span>
                <span className="font-semibold text-body">{res.win_pct}%</span>
              </div>
              <div>
                <span className="text-muted block text-[11px]">Trades</span>
                <span className="font-semibold text-body">{res.total_trades} ({res.wins}W/{res.losses}L)</span>
              </div>
              <div>
                <span className="text-muted block text-[11px]">Profit Factor</span>
                <span className={`font-semibold text-body ${res.profit_factor >= 1.3 ? 'text-green' : 'text-ink'}`}>
                  {res.profit_factor} <span className="text-[10px] text-muted font-normal">(≥1.3 req)</span>
                </span>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-2 text-center text-xs num py-2 mt-2">
              <div>
                <span className="text-muted block text-[11px]">Gross P&amp;L</span>
                <span className={pnlColor(res.gross_pnl)}>{rupee(res.gross_pnl, true)}</span>
              </div>
              <div>
                <span className="text-muted block text-[11px]">Costs &amp; Slippage</span>
                <span className="text-muted">{rupee(res.total_costs)}</span>
              </div>
              <div>
                <span className="text-muted block text-[11px]">Max Drawdown</span>
                <span className="text-red">{rupee(res.max_drawdown)}</span>
              </div>
            </div>
          </Card>

          {/* Interactive 23-Point Gauntlet Checklist Explorer */}
          {checklist23 && <GauntletChecklistExplorer checklist23={checklist23} />}

          {/* Trade Replay Log */}
          {(res.trades || []).length > 0 && <TradeReplayLog trades={res.trades} />}
        </div>
      )}
    </div>
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
  const h = s?.health || {};

  useEffect(() => {
    api.getDataStatus().then(setDataStatus).catch(() => {});
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
                <span className={`chip text-[10px] ${fresh ? 'bg-green-soft text-green border-green' : 'bg-amber-soft text-amber border-amber'}`}>
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
          <span className={`chip text-[10px] font-semibold ${
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
            <div className="font-mono text-[11px] opacity-90">{dataStatus.broker_auth_error}</div>
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
    </div>
  );
}

/* ---------------------------------------------------------------- System */

export function System({ s, onKill, onEnablePush, pushState, onOpenConfig, onOpenJournal, onRefresh }) {
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
      </Card>

      {/* Risk Profile Presets */}
      <Card>
        <Eyebrow>Risk Profile Presets</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          1-tap risk budget preset selection for market conditions (Target / Loss Limit / Risk per trade).
        </p>
        <div className="grid grid-cols-3 gap-2 mt-3">
          {['CONSERVATIVE', 'MODERATE', 'AGGRESSIVE'].map((p) => (
            <button
              key={p}
              className={`btn text-xs py-2 ${
                (s.risk?.target === (p === 'CONSERVATIVE' ? 1500 : p === 'AGGRESSIVE' ? 5000 : 2500))
                  ? 'bg-green text-paper border-green font-bold'
                  : 'btn-ghost'
              }`}
              onClick={async () => {
                try {
                  await api.setPreset(p);
                  alert(`Applied ${p} risk profile preset!`);
                  if (onRefresh) onRefresh();
                } catch (e) {
                  alert(e.message);
                }
              }}
            >
              {p}
            </button>
          ))}
        </div>
      </Card>

      {/* Parameters */}
      <Card>
        <Eyebrow>System Parameters</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          Every tunable — target, loss limit, risk, windows, sizing, guardian, events.
          Editable outside 09:15–15:30 IST.
        </p>
        <button className="btn bg-ink text-paper mt-3" onClick={onOpenConfig}>
          Open config
        </button>
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

      {/* Trading Journal */}
      <Card>
        <Eyebrow>Trading Journal &amp; Day-End Reports</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          Review historical daily P&amp;L reports, violations log, and closed trade analytics.
        </p>
        <button className="btn-ghost mt-3" onClick={onOpenJournal}>
          Read Day-End Report &amp; Journal
        </button>
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- Config

   Every value in params.yaml, editable from the phone. The server refuses writes
   between 09:15 and 15:30 IST (deliberate friction — you cannot loosen a risk rule
   mid-tilt), so during market hours this is a read-only view.                    */

function leaves(obj, base = []) {
  const out = [];
  for (const [k, v] of Object.entries(obj || {})) {
    const path = [...base, k];
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) out.push(...leaves(v, path));
    else out.push({ path, value: v });
  }
  return out;
}

const at = (obj, path) => path.reduce((o, k) => (o == null ? o : o[k]), obj);

function setAt(obj, path, value) {
  let node = obj;
  for (const k of path.slice(0, -1)) node = node[k];
  node[path.at(-1)] = value;
}

/* The default value is the schema: a number stays a number, a flag stays a bool.
   Without this a typed "12" would be written back to YAML as the string "12". */
function coerce(text, sample) {
  if (typeof sample === 'number') {
    const n = Number(text);
    if (text.trim() === '' || !Number.isFinite(n)) return { ok: false };
    return { ok: true, value: Number.isInteger(sample) && Number.isInteger(n) ? n : n };
  }
  if (Array.isArray(sample)) {
    try {
      return { ok: true, value: JSON.parse(text) };
    } catch {
      return { ok: false };
    }
  }
  return { ok: true, value: text };
}

export function Config({ onBack, flash }) {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState(null);
  const [buf, setBuf] = useState({});
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');

  const load = () =>
    api
      .getConfig()
      .then((d) => {
        setData(d);
        setDraft(structuredClone(d.params));
        setBuf({});
        setErr('');
      })
      .catch((e) => setErr(e.message));

  useEffect(() => {
    load();
  }, []);

  if (err) {
    return (
      <>
        <button className="btn-ghost" onClick={onBack}>← Back</button>
        <Card><Empty>{err}</Empty></Card>
      </>
    );
  }
  if (!draft) {
    return (
      <>
        <button className="btn-ghost" onClick={onBack}>← Back</button>
        <Card><Empty>Loading config…</Empty></Card>
      </>
    );
  }

  const editable = data.editable_now;
  const dirty = JSON.stringify(draft) !== JSON.stringify(data.params);
  const invalid = Object.entries(buf).filter(([, v]) => v.bad).map(([k]) => k);

  const edit = (path, text) => {
    const key = path.join('.');
    const sample = at(data.defaults, path) ?? at(data.params, path);
    const c = coerce(text, sample);
    setBuf({ ...buf, [key]: { text, bad: !c.ok } });
    if (!c.ok) return;
    const next = structuredClone(draft);
    setAt(next, path, c.value);
    setDraft(next);
  };

  const toggle = (path) => {
    const next = structuredClone(draft);
    setAt(next, path, !at(draft, path));
    setDraft(next);
  };

  const save = async () => {
    setBusy('save');
    try {
      const r = await api.putConfig(draft);
      setData({ ...data, params: r.params });
      setDraft(structuredClone(r.params));
      setBuf({});
      flash(`Saved. ${r.note}`);
    } catch (e) {
      flash(e.message);
    } finally {
      setBusy('');
    }
  };

  const reset = async () => {
    if (!window.confirm('Restore EVERY parameter to factory defaults?')) return;
    setBusy('reset');
    try {
      const r = await api.resetConfig();
      setData({ ...data, params: r.params });
      setDraft(structuredClone(r.params));
      setBuf({});
      flash(`Reset to defaults. ${r.note}`);
    } catch (e) {
      flash(e.message);
    } finally {
      setBusy('');
    }
  };

  const groups = Object.keys(draft);

  return (
    <div className="space-y-cardgap">
      <div className="flex items-center justify-between">
        <button className="btn-ghost" onClick={onBack}>← Back</button>
        {dirty && <span className="chip bg-amber-soft text-amber border-amber">unsaved</span>}
      </div>

      {!editable && (
        <Banner tone="amber">
          READ-ONLY 09:15–15:30 IST. Risk rules cannot be changed mid-session by design.
        </Banner>
      )}

      {groups.map((g) => {
        const rows = typeof draft[g] === 'object' && draft[g] !== null && !Array.isArray(draft[g])
          ? leaves(draft[g], [g])
          : [{ path: [g], value: draft[g] }];
        return (
          <Card key={g}>
            <Eyebrow>{g.replace(/_/g, ' ')}</Eyebrow>
            <div className="mt-2 divide-y divide-line">
              {rows.map(({ path, value }) => {
                const key = path.join('.');
                const label = path.slice(g === path[0] ? 1 : 0).join(' · ') || g;
                const def = at(data.defaults, path);
                const changed = JSON.stringify(value) !== JSON.stringify(def);
                const shown = buf[key]?.text ?? (Array.isArray(value) ? JSON.stringify(value) : String(value));
                return (
                  <div key={key} className="py-3 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-body truncate">{label}</div>
                      {changed && def !== undefined && (
                        <div className="num text-eyebrow text-muted mt-1">
                          default {String(def)}
                        </div>
                      )}
                    </div>
                    {typeof value === 'boolean' ? (
                      <button
                        className={`chip ${value ? 'bg-green text-paper border-green' : 'bg-ink text-paper border-ink'}`}
                        disabled={!editable}
                        onClick={() => toggle(path)}
                      >
                        {value ? 'on' : 'off'}
                      </button>
                    ) : (
                      <input
                        className={`num rounded-block px-3 py-2 text-right w-32 border ${
                          buf[key]?.bad ? 'border-red text-red' : 'border-line'
                        }`}
                        inputMode={typeof value === 'number' ? 'decimal' : 'text'}
                        disabled={!editable}
                        value={shown}
                        onChange={(e) => edit(path, e.target.value)}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </Card>
        );
      })}

      <Card>
        <Eyebrow>Apply</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          Windows, sizing, guardian and event settings take effect immediately. Risk-engine
          values (target, loss limit, risk per trade, FSM floors) load at boot — restart to
          apply those.
        </p>
        <div className="mt-4 space-y-2">
          <button
            className="btn bg-ink text-paper w-full"
            disabled={!editable || !dirty || invalid.length > 0 || busy === 'save'}
            onClick={save}
          >
            {busy === 'save' ? 'Saving…' : 'Save all'}
          </button>
          {invalid.length > 0 && (
            <div className="num text-sec text-red">invalid: {invalid.join(', ')}</div>
          )}
          <button
            className="btn-ghost w-full"
            disabled={!editable || busy === 'reset'}
            onClick={reset}
          >
            {busy === 'reset' ? 'Resetting…' : 'Reset all to defaults'}
          </button>
        </div>
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
            style={{ background: ok ? '#0E9F6E' : '#D97706' }}
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
  if (!r) return <Card><Empty>Loading…</Empty></Card>;
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

      <Card>
        <Eyebrow>Executed Trades ({r.replay?.length || 0})</Eyebrow>
        {(r.replay || []).length === 0 ? (
          <Empty>No trades executed today.</Empty>
        ) : (
          <div className="mt-3 space-y-3">
            {r.replay.map((t, i) => (
              <div key={i} className="p-3 rounded-block bg-line/30 border border-line/60 space-y-1.5">
                <div className="flex justify-between items-center font-disp font-semibold text-sec">
                  <span>{t.symbol}</span>
                  <span className={pnlColor(t.net)}>{rupee(t.net, true)}</span>
                </div>
                <div className="num text-sec text-muted flex justify-between">
                  <span>BUY @ ₹{t.entry} → SELL @ ₹{t.exit}</span>
                  <span>Gross: {rupee(t.gross, true)}</span>
                </div>
                <div className="num text-ai text-muted flex justify-between text-xs pt-1 border-t border-line/40">
                  <span>Origin: {t.origin} ({t.reason || 'manual'})</span>
                  <span>Charges: {rupee(t.costs)}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

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

