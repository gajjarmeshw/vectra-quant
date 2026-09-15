import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Home, Zap, FlaskConical, Database, Settings, Ghost } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import * as api from './api.js';
import { Banner, StateChip } from './components.jsx';
import {
  DayEnd, Journal, Locked, Positions, Signals, System, Today,
  Strategies, Backtest, DataScreen, PaperTrading
} from './screens.jsx';

const TABS = [
  { id: 'home', label: 'Home', icon: Home },
  { id: 'paper', label: 'Paper', icon: Ghost },
  { id: 'trade', label: 'Trade', icon: Zap },
  { id: 'backtest', label: 'Backtest', icon: FlaskConical },
  { id: 'data', label: 'Data', icon: Database },
  { id: 'settings', label: 'System', icon: Settings },
];

export default function App() {
  const [tab, setTab] = useState('home');
  const [state, setState] = useState(null);
  const [conn, setConn] = useState('connecting');
  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState('');
  const [pushState, setPushState] = useState('');
  const [showDayEnd, setShowDayEnd] = useState(false);
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

  // Push survives relaunches; the button label did not. Ask the browser what the
  // real state is, and re-register with the server while we are here.
  useEffect(() => {
    api.pushStatus().then((s) => s && setPushState(s));
  }, []);

  useEffect(() => {
    refresh();
    closeRef.current = api.liveSocket(
      (payload) => {
        setState(payload);
        setConn('connected');
      },
      (s) => setConn(s),
    );
    // Polling backstop: if the socket is wedged, the console must not go stale.
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
    // Terminal screen: the tab bar has not rendered yet, so this must say what is
    // wrong and offer the one action that can fix it.
    return (
      <div className="min-h-screen grid place-items-center px-gutter">
        <div className="text-center max-w-xs">
          <img src="/logo.svg" alt="" width="56" height="56" className="mx-auto opacity-90" />
          <div className="num text-sec text-muted mt-4">
            {conn === 'error' ? 'Backend unreachable' : 'Connecting to SENTINEL…'}
          </div>
          {toast && <div className="num text-eyebrow text-red mt-2">{toast}</div>}
          <button className="btn-ghost mt-6" onClick={refresh}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const locked = state.fsm?.state === 'LOCKED';
  const healthy =
    state.kill_switch === 'ON' && !state.health?.feed_degraded && conn !== 'error';

  return (
    <div className="min-h-screen bg-paper flex flex-col md:flex-row">
      {/* Mobile Tab Bar */}
      <nav className="md:hidden tabbar w-full flex justify-around items-center px-2 z-40 bg-card border-t border-line">
        {TABS.map((t) => {
          const active = tab === t.id;
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              onClick={() => {
                setTab(t.id);
                setShowDayEnd(false);
              }}
              className="relative flex flex-col items-center gap-1 px-1 py-2 shrink-0 w-16"
            >
              <div className="relative">
                <Icon 
                  size={22} 
                  strokeWidth={active ? 2.5 : 2} 
                  className={`transition-colors duration-200 ${active ? 'text-ink' : 'text-muted'}`} 
                />
                {active && (
                  <motion.div 
                    layoutId="tab-indicator-mobile"
                    className="absolute -bottom-1.5 left-1/2 -translate-x-1/2 w-1.5 h-1.5 rounded-full bg-ink"
                    transition={{ type: "spring", stiffness: 300, damping: 20 }}
                  />
                )}
              </div>
              <span
                className={`num font-medium tracking-wide transition-colors ${
                  active ? 'text-ink' : 'text-muted'
                }`}
                style={{ fontSize: '10px' }}
              >
                {t.label}
              </span>
            </button>
          );
        })}
      </nav>

      {/* Desktop Sidebar */}
      <aside className="hidden md:flex flex-col w-64 border-r border-line bg-card shrink-0">
        <div className="p-6">
          <div className="font-disp text-ink font-bold tracking-tight flex items-center gap-2 text-xl">
            <div className="w-8 h-8 rounded-full bg-ai grid place-items-center shadow-lg">
              <span className="text-white text-[15px]">S</span>
            </div>
            SENTINEL
            <span
              className="inline-block w-2 h-2 rounded-full ml-1"
              style={
                healthy
                  ? { background: '#10B981', boxShadow: '0 0 8px rgba(16,185,129,0.4)' }
                  : { background: '#EF4444', boxShadow: '0 0 8px rgba(239,68,68,0.4)' }
              }
            />
          </div>
        </div>
        <nav className="flex-1 px-4 space-y-2 mt-4">
          {TABS.map((t) => {
            const active = tab === t.id;
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                onClick={() => {
                  setTab(t.id);
                  setShowDayEnd(false);
                }}
                className={`relative flex items-center gap-3 w-full px-4 py-3 rounded-xl transition-all duration-200 ${
                  active ? 'bg-line-soft text-ink font-semibold' : 'text-muted hover:text-ink hover:bg-line-soft/50'
                }`}
              >
                <Icon size={20} strokeWidth={active ? 2.5 : 2} />
                <span className="font-disp tracking-wide">{t.label}</span>
                {active && (
                  <motion.div 
                    layoutId="tab-indicator-desktop"
                    className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 rounded-r-full bg-ink"
                    transition={{ type: "spring", stiffness: 300, damping: 20 }}
                  />
                )}
              </button>
            );
          })}
        </nav>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0 h-screen overflow-y-auto pb-24 md:pb-0">
        <header className="md:hidden flex items-center justify-between px-gutter pt-6 pb-4">
          <div className="font-disp text-ink font-bold tracking-tight flex items-center gap-2 text-xl">
            <div className="w-8 h-8 rounded-full bg-ai grid place-items-center shadow-lg">
              <span className="text-white text-[15px]">S</span>
            </div>
            SENTINEL
            <span
              className="inline-block w-2 h-2 rounded-full ml-1"
              style={
                healthy
                  ? { background: '#10B981', boxShadow: '0 0 8px rgba(16,185,129,0.4)' }
                  : { background: '#EF4444', boxShadow: '0 0 8px rgba(239,68,68,0.4)' }
              }
            />
          </div>
          <StateChip state={state.fsm?.state} floor={state.fsm?.floor} />
        </header>

        <div className="hidden md:flex items-center justify-end px-8 pt-6 pb-2 border-b border-line/50 mb-6 sticky top-0 bg-paper/80 backdrop-blur-md z-30">
          <StateChip state={state.fsm?.state} floor={state.fsm?.floor} />
        </div>

        <main className="px-gutter md:px-12 w-full max-w-6xl space-y-cardgap pb-12">
          {conn === 'reconnecting' && (
            <div className="num text-eyebrow text-center py-1 bg-amber-soft text-amber rounded mb-4">
              reconnecting…
            </div>
          )}
          {toast && <Banner tone="ink">{toast}</Banner>}

        {showDayEnd ? (
          <>
            <button className="btn-ghost" onClick={() => setShowDayEnd(false)}>
              ← Back
            </button>
            <DayEnd />
          </>
        ) : (
          <div className="relative">
            <div className={tab === 'home' ? 'block animate-in fade-in slide-in-from-bottom-2 duration-300' : 'hidden'}>
              <div className="space-y-cardgap">
                {locked ? (
                  <Locked s={state} onReadReport={() => setShowDayEnd(true)} />
                ) : (
                  <Today s={state} onSquareOff={onSquareOff} onReadReport={() => setShowDayEnd(true)} />
                )}
                <div className="pt-4 border-t border-line">
                  <h3 className="font-disp font-semibold text-lg text-ink mb-3 px-1">Active Positions</h3>
                  <Positions s={state} onSquareOff={onSquareOff} busy={busy === 'squareoff'} />
                </div>
              </div>
            </div>
            
            <div className={tab === 'paper' ? 'block animate-in fade-in slide-in-from-bottom-2 duration-300' : 'hidden'}>
              <div className="space-y-cardgap">
                <PaperTrading s={state} />
              </div>
            </div>

            <div className={tab === 'trade' ? 'block animate-in fade-in slide-in-from-bottom-2 duration-300' : 'hidden'}>
              <div className="space-y-cardgap">
                <Signals s={state} onApprove={onApprove} onReject={onReject} busy={busy} />
                <div className="pt-4 border-t border-line">
                  <h3 className="font-disp font-semibold text-lg text-ink mb-3 px-1">Algo Strategies</h3>
                  <Strategies s={state} onRefresh={refresh} />
                </div>
              </div>
            </div>

            <div className={tab === 'backtest' ? 'block animate-in fade-in slide-in-from-bottom-2 duration-300' : 'hidden'}>
              <div className="space-y-cardgap">
                <Backtest s={state} />
              </div>
            </div>

            <div className={tab === 'data' ? 'block animate-in fade-in slide-in-from-bottom-2 duration-300' : 'hidden'}>
              <div className="space-y-cardgap">
                <DataScreen s={state} onRefresh={refresh} />
              </div>
            </div>

            <div className={tab === 'settings' ? 'block animate-in fade-in slide-in-from-bottom-2 duration-300' : 'hidden'}>
              <System
                s={state}
                onKill={onKill}
                onEnablePush={onEnablePush}
                pushState={pushState}
                onOpenJournal={() => setShowDayEnd(true)}
                onRefresh={refresh}
              />
            </div>
          </div>
        )}
        </main>
      </div>
    </div>
  );
}
