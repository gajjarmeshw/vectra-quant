import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './index.css';

// One-shot cleanup: a build that shipped a demo mode left this key on devices that
// used it. Nothing reads it any more; clear it so no stale flag can linger.
try {
  localStorage.removeItem('sentinel.demo');
} catch {
  /* private mode / storage disabled */
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      /* a failed SW registration must not block the console */
    });
  });
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
