/* Server-render every screen with a representative state object.
 *
 * There is no component test suite, so this is the cheap gate that catches the
 * failure mode that actually bites during a refactor: a screen that no longer
 * renders at all (missing export, renamed prop, bad JSX). It does not assert
 * appearance — effects and IntersectionObserver never run under SSR.
 *
 *   npm run smoke
 */
import React from 'react';
import { renderToString } from 'react-dom/server';
import App from '../src/App.jsx';
import * as S from '../src/screens.jsx';

const state = {
  mode: 'PAPER', broker: 'dhan', kill_switch: 'ON', session_date: '2026-09-18',
  violations_today: 0, week_locked: false, in_entry_window: true,
  fsm: { state: 'ACTIVE', day_pnl: 1234.5, floor: 0, target: 5000, loss_limit: 3000, session_date: '2026-09-18' },
  trades: [{ id: 1, status: 'CLOSED', symbol: 'NIFTY25000CE', entry: 100, exit: 120, reason: 'TARGET', costs: 40, pnl: 1460, closed_at: '2026-09-18T04:30:00Z' }],
  positions: [], active_suggestions: [], market: { NIFTY: { ltp: 25000, change_pct: 0.4 } },
  health: {}, algo: {}, expiry_today: [],
};

const cases = {
  App: <App />,
  Today: <S.Today s={state} onSquareOff={() => {}} />,
  Locked: <S.Locked s={{ ...state, fsm: { ...state.fsm, state: 'LOCKED', lock_reason: 'target hit' } }} onReadReport={() => {}} />,
  Positions: <S.Positions s={state} onSquareOff={() => {}} busy={false} />,
  Signals: <S.Signals s={state} onApprove={() => {}} onReject={() => {}} busy="" />,
  Reports: <S.Reports />,
  Strategies: <S.Strategies s={state} onRefresh={() => {}} />,
  System: <S.System s={state} onKill={() => {}} onEnablePush={() => {}} pushState="" onRefresh={() => {}} />,
  Backtest: <S.Backtest s={state} />,
  DataScreen: <S.DataScreen s={state} onRefresh={() => {}} />,
};

let bad = 0;
for (const [name, el] of Object.entries(cases)) {
  try {
    const html = renderToString(el);
    console.log(`PASS ${name.padEnd(12)} ${html.length} chars`);
  } catch (e) {
    bad += 1;
    console.log(`FAIL ${name.padEnd(12)} ${e.message}`);
  }
}
process.exit(bad ? 1 : 0);
