/* The seven screens. docs/design_spec.md §3. */
import React, { useEffect, useState } from 'react';
import {
  Banner, Card, DayRail, Empty, Eyebrow, OriginTag, Row, SlTrack, Stat, StateChip,
  TradeDots, num, pnlColor, rupee,
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

export function Signals({ s, onApprove, onReject, busy }) {
  const active = s.active_suggestions || [];
  return (
    <div className="space-y-cardgap">
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
    TAKEN: 'bg-ink text-white border-ink',
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

  return (
    <Card accent className={dead ? 'opacity-50' : ''}>
      <div className="-m-cardpad mb-cardpad px-cardpad py-2.5 bg-ai-soft rounded-t-card flex justify-between items-center">
        <span className="num text-eyebrow text-ai flex items-center gap-2">
          <span className="inline-block w-[7px] h-[7px] rounded-full bg-ai ai-pulse" />
          {(q.model || 'LLM').split('/').pop().toUpperCase()}
          {q.event ? ` · ${q.event}` : ''}
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

      <button className="btn-primary" onClick={onReadReport}>
        Read day-end report
      </button>
      <a
        className="btn-ghost block text-center"
        href="https://groww.in/user/profile/settings"
        target="_blank"
        rel="noreferrer"
      >
        Groww kill switch ↗
      </a>

      <Banner tone="ink">Your broker-side stops remain resting.</Banner>
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

/* ---------------------------------------------------------------- System */

export function System({ s, onKill, onEnablePush, pushState, onOpenConfig }) {
  const [typed, setTyped] = useState('');
  const on = s.kill_switch === 'ON';
  const h = s.health || {};
  return (
    <div className="space-y-cardgap">
      <div className="card bg-ink text-white border-ink">
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
            className="btn bg-red text-white mt-4"
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
              className="btn bg-green text-white"
              disabled={typed.trim() !== 'ENABLE'}
              onClick={() => onKill('ON', { typed })}
            >
              Re-enable
            </button>
          </div>
        )}
      </div>

      <Card>
        <Eyebrow>Connection health</Eyebrow>
        <div className="mt-3 space-y-2">
          <HealthRow
            label="Market data"
            ok={!h.feed_degraded}
            detail={`${h.feed_subscribed ?? 0} symbols · ${num(h.feed_age_s, 1)}s`}
          />
          <HealthRow
            label="Option chain"
            ok={Object.values(h.chain_fresh || {}).every(Boolean)}
            detail={Object.entries(h.chain_fresh || {})
              .map(([k, v]) => `${k} ${v ? 'ok' : 'stale'}`)
              .join(' · ')}
          />
          <HealthRow
            label="LLM"
            ok={(s.llm?.failures ?? 0) === 0}
            detail={`${s.llm?.calls_today ?? 0}/${s.llm?.cap ?? 10} calls`}
          />
          <HealthRow label="Guardian" ok detail={h.guardian_detection} />
        </div>
      </Card>

      <Card>
        <Row
          left={<Eyebrow>Mode</Eyebrow>}
          right={
            <span className={`chip ${s.mode === 'LIVE' ? 'bg-green text-white border-green' : 'bg-ink text-white border-ink'}`}>
              {s.mode}
            </span>
          }
        />
        <div className="num text-sec text-muted mt-3">
          Session {s.fsm?.session_date} · violations today {s.violations_today ?? 0}
          {s.week_locked && <span className="text-red"> · WEEK LOCKED</span>}
        </div>
      </Card>

      <Card>
        <Eyebrow>Notifications</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          iOS requires this app to be installed to the home screen before push works.
        </p>
        <button className="btn-ghost mt-3" onClick={onEnablePush}>
          {pushState || 'Enable push'}
        </button>
      </Card>

      <Card>
        <Eyebrow>Parameters</Eyebrow>
        <p className="text-body text-ink-2 mt-2">
          Every tunable — target, loss limit, risk, windows, sizing, guardian, events.
          Editable outside 09:15–15:30 IST.
        </p>
        <button className="btn bg-ink text-white mt-3" onClick={onOpenConfig}>
          Open config
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
                        className={`chip ${value ? 'bg-green text-white border-green' : 'bg-ink text-white border-ink'}`}
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
            className="btn bg-ink text-white w-full"
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
