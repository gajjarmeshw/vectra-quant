import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as api from './api.js';
import { Banner, StateChip } from './components.jsx';
import { Config, DayEnd, Journal, Locked, Positions, Signals, System, Today } from './screens.jsx';

const TABS = [
  { id: 'today', label: 'Today', glyph: '◉' },
  { id: 'positions', label: 'Positions', glyph: '▤' },
  { id: 'signals', label: 'Signals', glyph: '✦' },
  { id: 'journal', label: 'Journal', glyph: '≡' },
  { id: 'system', label: 'System', glyph: '◻' },
];

export default function App() {
  const [tab, setTab] = useState('today');
  const [state, setState] = useState(null);
  const [conn, setConn] = useState('connecting');
  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState('');
  const [pushState, setPushState] = useState('');
  const [showDayEnd, setShowDayEnd] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
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
      const r = await api.squareOff();
      flash(r.flat ? 'Flat — verified' : 'NOT FLAT — check the Groww app now');
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
    <div className="min-h-screen pb-32">
      {conn === 'reconnecting' && (
        <div className="num text-eyebrow text-center py-1 bg-amber-soft text-amber">
          reconnecting…
        </div>
      )}

      <header className="flex items-center justify-between px-gutter pt-4 pb-3">
        <div className="font-disp text-brand font-bold tracking-tight flex items-center gap-2">
          <img src="/logo.svg" alt="" width="22" height="22" />
          SENTINEL
          {/* Live-ness at a glance: green = armed, red = kill switch off or data down. */}
          <span
            className="inline-block w-2 h-2 rounded-full ml-0.5"
            style={
              healthy
                ? { background: '#0E9F6E', boxShadow: '0 0 0 4px #E6F6F0' }
                : { background: '#E5484D', boxShadow: '0 0 0 4px #FDEBEC' }
            }
          />
        </div>
        <StateChip state={state.fsm?.state} floor={state.fsm?.floor} />
      </header>

      <main className="px-gutter space-y-cardgap">
        {toast && <Banner tone="ink">{toast}</Banner>}

        {showConfig ? (
          <Config onBack={() => setShowConfig(false)} flash={flash} />
        ) : showDayEnd ? (
          <>
            <button className="btn-ghost" onClick={() => setShowDayEnd(false)}>
              ← Back
            </button>
            <DayEnd />
          </>
        ) : (
          <>
            {tab === 'today' &&
              (locked ? (
                <Locked s={state} onReadReport={() => setShowDayEnd(true)} />
              ) : (
                <Today s={state} onSquareOff={onSquareOff} />
              ))}
            {tab === 'positions' && (
              <Positions s={state} onSquareOff={onSquareOff} busy={busy === 'squareoff'} />
            )}
            {tab === 'signals' && (
              <Signals s={state} onApprove={onApprove} onReject={onReject} busy={busy} />
            )}
            {tab === 'journal' && (
              <>
                <button className="btn-ghost" onClick={() => setShowDayEnd(true)}>
                  Read day-end report
                </button>
                <Journal />
              </>
            )}
            {tab === 'system' && (
              <System
                s={state}
                onKill={onKill}
                onEnablePush={onEnablePush}
                pushState={pushState}
                onOpenConfig={() => setShowConfig(true)}
              />
            )}
          </>
        )}
      </main>

      <nav className="tabbar flex items-start justify-around pt-3">
        {TABS.map((t) => {
          const active = tab === t.id && !showDayEnd && !showConfig;
          return (
            <button
              key={t.id}
              onClick={() => {
                setTab(t.id);
                setShowDayEnd(false);
                setShowConfig(false);
              }}
              className="flex flex-col items-center gap-1 px-3"
              style={{ minWidth: 44, minHeight: 44 }}
            >
              <span className={`text-base ${active ? 'text-ink' : 'text-muted'}`}>{t.glyph}</span>
              <span
                className="num"
                style={{ fontSize: '9.5px', color: active ? '#0B0B0A' : '#8A8A85' }}
              >
                {t.label}
              </span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
