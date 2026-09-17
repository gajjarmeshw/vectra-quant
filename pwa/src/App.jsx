import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Home, Zap, FlaskConical, Database, Settings, FileText, Wifi, WifiOff } from 'lucide-react';
import { motion } from 'framer-motion';
import * as api from './api.js';
import { Banner, StateChip, num, pnlColor, useFlash } from './components.jsx';
import AmbientField from './ambient.jsx';
import {
  Locked, Positions, Signals, System, Today,
  Strategies, Backtest, DataScreen, Reports
} from './screens.jsx';

/* One destination per job-to-be-done. `Paper` used to re-render the whole of
   Home (duplicate P&L, premarket and closed orders); mode now lives in the
   sidebar + System, and reports/history consolidate into Reports. */
const TABS = [
  { id: 'home', label: 'Home', icon: Home, title: 'Today' },
  { id: 'trade', label: 'Trade', icon: Zap, title: 'Trade' },
  { id: 'backtest', label: 'Backtest', icon: FlaskConical, title: 'Backtest' },
  { id: 'reports', label: 'Reports', icon: FileText, title: 'Reports & History' },
  { id: 'data', label: 'Data', icon: Database, title: 'Market Data' },
  { id: 'settings', label: 'System', icon: Settings, title: 'System' },
];

const TICKERS = [
  { key: 'NIFTY', label: 'NIFTY', digits: 0 },
  { key: 'SENSEX', label: 'SENSEX', digits: 0 },
  { key: 'INDIAVIX', label: 'INDIA VIX', digits: 2 },
];

/* One ticker cell — flashes green/red the instant its price ticks, so a
   change is noticeable without staring at the strip. */
function TickerCell({ label, quote = {}, digits }) {
  const ltp = quote.ltp == null ? null : Number(quote.ltp);
  const flash = useFlash(ltp);
  const ch = quote.change_pct;
  const up = ch > 0;
  return (
    <div className="ticker-cell py-2 flex-1 md:flex-none justify-center md:justify-start">
      <span className="eyebrow">{label}</span>
      <span className={`num text-sec font-medium text-ink rounded px-1 -mx-0.5 ${flash}`}>
        {ltp == null ? '—' : ltp.toLocaleString('en-IN', { maximumFractionDigits: digits })}
      </span>
      <span className={`num text-eyebrow ${pnlColor(ch)}`}>
        {ch == null ? '' : `${up ? '▲' : ch < 0 ? '▼' : ''}${Math.abs(num(ch, 2))}%`}
      </span>
    </div>
  );
}

/* Always-visible market strip — the one thing a trading console should never
   make you navigate to find. */
function TickerStrip({ market = {} }) {
  return (
    <div className="flex items-stretch overflow-x-auto no-scrollbar w-full md:w-auto">
      {TICKERS.map(({ key, label, digits }) => (
        <TickerCell key={key} label={label} quote={market[key] || {}} digits={digits} />
      ))}
    </div>
  );
}

function ConnDot({ conn, healthy }) {
  const ok = conn === 'connected' && healthy;
  const Icon = conn === 'error' ? WifiOff : Wifi;
  const tone = conn === 'error' ? 'text-red' : ok ? 'text-green' : 'text-amber';
  return (
    <span className={`inline-flex items-center gap-1.5 num text-eyebrow ${tone}`} title={`socket: ${conn}`}>
      <Icon size={12} strokeWidth={2.4} />
      {conn === 'connected' ? 'LIVE' : conn === 'error' ? 'OFFLINE' : 'SYNCING'}
    </span>
  );
}

function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span className="num text-eyebrow text-muted tabular-nums">
      {now.toLocaleTimeString('en-IN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false, timeZone: 'Asia/Kolkata',
      })} IST
    </span>
  );
}

function Brand({ healthy, compact = false }) {
  return (
    <div className="flex items-center gap-2.5">
      <div
        className="w-7 h-7 rounded-lg grid place-items-center shrink-0"
        style={{ background: 'linear-gradient(135deg,#4D8DFF,#2B6BE0)', boxShadow: '0 0 16px -4px rgba(77,141,255,0.6)' }}
      >
        <span className="font-disp text-white text-f13 font-bold">V</span>
      </div>
      {!compact && (
        <div className="min-w-0">
          <div className="font-disp text-brand font-bold text-ink leading-none">VECTRA</div>
          <div className="eyebrow mt-1">QUANT TERMINAL</div>
        </div>
      )}
      <span
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={
          healthy
            ? { background: '#00D98B', boxShadow: '0 0 8px rgba(0,217,139,0.7)' }
            : { background: '#FF4D5E', boxShadow: '0 0 8px rgba(255,77,94,0.7)' }
        }
      />
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState('home');
  const [state, setState] = useState(null);
  const [conn, setConn] = useState('connecting');
  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState('');
  const [pushState, setPushState] = useState('');
  const [btJob, setBtJob] = useState(null);
  const closeRef = useRef(null);

  const flash = useCallback((msg) => {
    setToast(msg);
    setTimeout(() => setToast(''), 4000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setState(await api.getState());
    } catch (e) {
      setConn('error');
      flash(e.message);
    }
  }, [flash]);

  useEffect(() => {
    api.pushStatus().then((s) => s && setPushState(s));
  }, []);

  useEffect(() => {
    refresh();
    closeRef.current = api.liveSocket(
      (payload) => {
        setConn('connected');
        if (!payload?.type || payload.type === 'state') {
          setState(payload);
        } else if (payload.type === 'backtest') {
          setBtJob(payload);
        }
      },
      (s) => setConn(s),
    );
    const t = setInterval(refresh, 15000);
    return () => {
      closeRef.current?.();
      clearInterval(t);
    };
  }, [refresh]);

  const onApprove = async (id) => {
    setBusy(id);
    try {
      const r = await api.approve(id);
      flash(`Placed ${r.lots} lot · risk ₹${Math.round(r.risk)}`);
      if (!r.sl_order_id) flash('WARNING: entry filled but stop was rejected — set it manually');
      await refresh();
    } catch (e) {
      flash(e.message);
    } finally {
      setBusy('');
    }
  };

  const onReject = async (id) => {
    setBusy(id);
    try {
      await api.reject(id, 'rejected in app');
      await refresh();
    } catch (e) {
      flash(e.message);
    } finally {
      setBusy('');
    }
  };

  const onSquareOff = async () => {
    if (!window.confirm('Market-exit every open position now?')) return;
    setBusy('squareoff');
    try {
      const brokerName = state?.broker === 'dhan' ? 'DhanHQ' : 'Groww';
      const r = await api.squareOff();
      flash(r.flat ? 'Flat — verified' : `NOT FLAT — check the ${brokerName} app now`);
      await refresh();
    } catch (e) {
      flash(e.message);
    } finally {
      setBusy('');
    }
  };

  const onKill = async (action, opts) => {
    try {
      await api.setKillSwitch(action, opts);
      flash(action === 'OFF' ? 'Kill switch OFF' : 'System re-enabled');
      await refresh();
    } catch (e) {
      flash(e.message);
    }
  };

  const onEnablePush = async () => {
    setPushState('Enabling…');
    try {
      await api.enablePush();
      setPushState('Push enabled ✓');
    } catch (e) {
      setPushState(e.message);
    }
  };

  if (!state) {
    return (
      <div className="min-h-screen grid place-items-center px-gutter">
        <div className="text-center max-w-xs">
          <div
            className="w-12 h-12 rounded-xl grid place-items-center mx-auto"
            style={{ background: 'linear-gradient(135deg,#4D8DFF,#2B6BE0)', boxShadow: '0 0 28px -6px rgba(77,141,255,0.7)' }}
          >
            <span className="font-disp text-white text-xl font-bold">V</span>
          </div>
          <div className="font-disp text-brand font-bold text-ink mt-4">VECTRA QUANT</div>
          <div className="num text-eyebrow text-muted mt-2">
            {conn === 'error' ? 'BACKEND UNREACHABLE' : 'CONNECTING…'}
          </div>
          {toast && <div className="num text-eyebrow text-red mt-3">{toast}</div>}
          <button className="btn-ghost mt-6" onClick={refresh}>Retry</button>
        </div>
      </div>
    );
  }

  const locked = state.fsm?.state === 'LOCKED';
  const healthy = state.kill_switch === 'ON' && !state.health?.feed_degraded && conn !== 'error';
  const activeTab = TABS.find((t) => t.id === tab) || TABS[0];

  const go = (id) => {
    setTab(id);
  };

  return (
    <div className="min-h-screen bg-paper flex flex-col md:flex-row">
      <AmbientField />

      {/* ---------- Mobile bottom tab bar ---------- */}
      {/* Six equal columns rather than fixed-width buttons — on a 360px phone
          the old w-14 pills left the row lopsided and under the 44px touch
          target. */}
      <nav className="md:hidden tabbar flex items-stretch z-40">
        {TABS.map((t) => {
          const active = tab === t.id;
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              onClick={() => go(t.id)}
              aria-label={t.title}
              aria-current={active ? 'page' : undefined}
              className="relative flex-1 min-w-0 flex flex-col items-center justify-center gap-1 px-0.5 py-2"
            >
              <Icon
                size={20}
                strokeWidth={active ? 2.5 : 2}
                className={`transition-colors ${active ? 'text-ai' : 'text-muted'}`}
              />
              <span
                className={`num w-full text-center truncate transition-colors ${
                  active ? 'text-ink' : 'text-muted'
                }`}
                style={{ fontSize: '9.5px', letterSpacing: '0.04em' }}
              >
                {t.label.toUpperCase()}
              </span>
              {active && (
                <motion.div
                  layoutId="tab-indicator-mobile"
                  className="absolute -top-[1px] left-1/2 -translate-x-1/2 w-7 h-[2px] rounded-full bg-ai"
                  style={{ boxShadow: '0 0 10px rgba(77,141,255,0.8)' }}
                  transition={{ type: 'spring', stiffness: 320, damping: 26 }}
                />
              )}
            </button>
          );
        })}
      </nav>

      {/* ---------- Desktop sidebar ---------- */}
      <aside className="relative z-20 hidden md:flex flex-col w-56 border-r border-line bg-card/70 backdrop-blur-xl shrink-0 h-screen sticky top-0">
        <div className="px-4 py-5 border-b border-line">
          <Brand healthy={healthy} />
        </div>

        <nav className="flex-1 px-2.5 py-3 space-y-0.5 overflow-y-auto">
          {TABS.map((t) => {
            const active = tab === t.id;
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                onClick={() => go(t.id)}
                className={`relative flex items-center gap-2.5 w-full px-3 py-2 rounded-block transition-colors duration-150 ${
                  active ? 'bg-card-2 text-ink' : 'text-ink-2 hover:text-ink hover:bg-card-2/60'
                }`}
              >
                <Icon size={16} strokeWidth={active ? 2.4 : 2} className={active ? 'text-ai' : ''} />
                <span className="font-disp text-btn font-medium">{t.label}</span>
                {active && (
                  <motion.div
                    layoutId="tab-indicator-desktop"
                    className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-r-full bg-ai"
                    style={{ boxShadow: '0 0 10px rgba(77,141,255,0.8)' }}
                    transition={{ type: 'spring', stiffness: 320, damping: 26 }}
                  />
                )}
              </button>
            );
          })}
        </nav>

        <div className="px-4 py-3 border-t border-line space-y-2">
          <div className="flex items-center justify-between">
            <ConnDot conn={conn} healthy={healthy} />
            <span className={`num text-eyebrow ${state.mode === 'LIVE' ? 'text-red' : 'text-ink-2'}`}>
              {state.mode || 'PAPER'}
            </span>
          </div>
          <Clock />
        </div>
      </aside>

      {/* ---------- Main ---------- */}
      <div className="relative z-10 flex-1 flex flex-col min-w-0 h-screen overflow-y-auto pb-24 md:pb-0">
        {/* sticky command bar: ticker + state, both platforms */}
        <div className="sticky top-0 z-30 bg-paper/70 backdrop-blur-xl border-b border-line">
          <div className="md:hidden flex items-center justify-between px-gutter pt-3 pb-2">
            <Brand healthy={healthy} compact />
            <div className="flex items-center gap-2">
              <ConnDot conn={conn} healthy={healthy} />
              <StateChip state={state.fsm?.state} />
            </div>
          </div>

          <div className="flex items-center justify-between gap-4 md:px-6">
            <TickerStrip market={state.market} />
            <div className="hidden md:flex items-center gap-3 shrink-0 pr-1">
              <Clock />
              <StateChip state={state.fsm?.state} />
            </div>
          </div>
        </div>

        <main className="px-gutter md:px-6 w-full max-w-[1560px] mx-auto py-4 md:py-6">
          {/* Where am I / what session. The phone only ever showed the logo, so
              the screen name had to be inferred from the tab bar. */}
          <div className="flex items-baseline justify-between gap-3 mb-3 md:mb-5">
            <h1 className="font-disp text-contract font-semibold text-ink tracking-tight truncate">
              {activeTab.title}
            </h1>
            <span className="num text-eyebrow text-muted shrink-0">
              SESSION {state.session_date || '—'}
            </span>
          </div>

          <div className="space-y-cardgap">
            {conn === 'reconnecting' && <Banner tone="amber">RECONNECTING…</Banner>}
            {toast && <Banner tone="ink">{toast}</Banner>}

            <div className="relative">
                <div className={tab === 'home' ? 'block animate-in fade-in slide-in-from-bottom-1 duration-200' : 'hidden'}>
                  {/* Dashboard grid: the day's state on the left, live risk
                      pinned in a right rail that stays put while you scroll. */}
                  <div className="grid grid-cols-1 xl:grid-cols-3 gap-3 items-start">
                    <div className="xl:col-span-2 space-y-cardgap min-w-0">
                      {locked ? (
                        <Locked s={state} onReadReport={() => go('reports')} />
                      ) : (
                        <Today s={state} onSquareOff={onSquareOff} />
                      )}
                    </div>
                    <div className="space-y-cardgap min-w-0 xl:sticky xl:top-[104px]">
                      <Positions s={state} onSquareOff={onSquareOff} busy={busy === 'squareoff'} />
                    </div>
                  </div>
                </div>

                <div className={tab === 'reports' ? 'block animate-in fade-in slide-in-from-bottom-1 duration-200' : 'hidden'}>
                  <Reports />
                </div>

                <div className={tab === 'trade' ? 'block animate-in fade-in slide-in-from-bottom-1 duration-200' : 'hidden'}>
                  <div className="grid grid-cols-1 xl:grid-cols-5 gap-3 items-start">
                    <div className="xl:col-span-2 space-y-cardgap min-w-0 xl:sticky xl:top-[104px]">
                      <Signals s={state} onApprove={onApprove} onReject={onReject} busy={busy} />
                    </div>
                    <div className="xl:col-span-3 space-y-cardgap min-w-0">
                      <Strategies s={state} onRefresh={refresh} />
                    </div>
                  </div>
                </div>

                <div className={tab === 'backtest' ? 'block animate-in fade-in slide-in-from-bottom-1 duration-200' : 'hidden'}>
                  <Backtest s={state} liveJob={btJob} />
                </div>

                <div className={tab === 'data' ? 'block animate-in fade-in slide-in-from-bottom-1 duration-200' : 'hidden'}>
                  <DataScreen s={state} onRefresh={refresh} />
                </div>

                <div className={tab === 'settings' ? 'block animate-in fade-in slide-in-from-bottom-1 duration-200' : 'hidden'}>
                  <System
                    s={state}
                    onKill={onKill}
                    onEnablePush={onEnablePush}
                    pushState={pushState}
                    onRefresh={refresh}
                  />
                </div>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
