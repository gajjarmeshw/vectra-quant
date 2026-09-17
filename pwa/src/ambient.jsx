/* The ambient field that sits behind the whole terminal.
 *
 * Three stacked layers on one canvas, all driven by a single rAF loop:
 *   1. two slow aurora blobs, so the black never reads as flat paint;
 *   2. a dot lattice that parallax-scrolls at 0.18x and brightens near the
 *      pointer — the surface you're scrolling *over*, not a static wallpaper;
 *   3. expanding rings on click, so a tap has a physical consequence.
 *
 * Deliberately cheap: one canvas, no DOM churn, capped DPR, and the loop is
 * suspended entirely when the tab is hidden or the OS asks for reduced motion.
 */
import { useEffect, useRef } from 'react';

const GRID = 34;          // lattice spacing, CSS px
const PARALLAX = 0.18;    // how much of the scroll the lattice absorbs
const POINTER_R = 190;    // radius of the pointer highlight
const RIPPLE_MS = 900;
const MAX_RIPPLES = 6;

export default function AmbientField() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) return undefined;

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

    let w = 0;
    let h = 0;
    let dpr = 1;
    const pointer = { x: -9999, y: -9999 };
    const ripples = [];
    let scrollY = 0;
    let raf = null;

    const resize = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    /* Scroll comes from the app's own scroll container, not the window — the
       shell uses `overflow-y-auto` on <main>, so window.scrollY never moves. */
    const readScroll = (e) => {
      const t = e?.target;
      scrollY = t && t !== document ? t.scrollTop || 0 : window.scrollY || 0;
    };

    const onPointer = (e) => {
      pointer.x = e.clientX;
      pointer.y = e.clientY;
    };
    const onLeave = () => {
      pointer.x = -9999;
      pointer.y = -9999;
    };
    const onClick = (e) => {
      if (ripples.length >= MAX_RIPPLES) ripples.shift();
      ripples.push({ x: e.clientX, y: e.clientY, t: performance.now() });
    };

    const aurora = (t) => {
      /* Two counter-drifting radial washes. Lissajous paths so they never
         land on a visibly repeating loop. */
      const blobs = [
        { x: 0.22 + 0.10 * Math.sin(t / 17000), y: 0.18 + 0.08 * Math.cos(t / 23000), c: '77,141,255', r: 0.62 },
        { x: 0.82 + 0.09 * Math.cos(t / 19000), y: 0.74 + 0.10 * Math.sin(t / 13000), c: '167,139,250', r: 0.55 },
      ];
      for (const b of blobs) {
        const cx = b.x * w;
        const cy = b.y * h;
        const rad = b.r * Math.max(w, h);
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad);
        g.addColorStop(0, `rgba(${b.c},0.055)`);
        g.addColorStop(0.5, `rgba(${b.c},0.018)`);
        g.addColorStop(1, `rgba(${b.c},0)`);
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, w, h);
      }
    };

    const lattice = (t) => {
      /* Offset wraps within one cell so the grid reads as infinite. */
      const off = ((-scrollY * PARALLAX) % GRID + GRID) % GRID;
      const breathe = 0.5 + 0.5 * Math.sin(t / 4200);

      for (let y = off - GRID; y < h + GRID; y += GRID) {
        for (let x = 0; x < w + GRID; x += GRID) {
          const dx = x - pointer.x;
          const dy = y - pointer.y;
          const d2 = dx * dx + dy * dy;
          const near = d2 < POINTER_R * POINTER_R ? 1 - Math.sqrt(d2) / POINTER_R : 0;

          let a = 0.05 + 0.02 * breathe + near * 0.42;
          let r = 1 + near * 1.3;

          /* Ripple fronts light the lattice as they pass through it. */
          for (const rp of ripples) {
            const age = (t - rp.t) / RIPPLE_MS;
            if (age >= 1) continue;
            const front = age * Math.max(w, h) * 0.7;
            const dist = Math.abs(Math.hypot(x - rp.x, y - rp.y) - front);
            if (dist < 46) {
              const hit = (1 - dist / 46) * (1 - age);
              a += hit * 0.85;
              r += hit * 1.6;
            }
          }

          if (a <= 0.052) {
            ctx.fillStyle = 'rgba(160,168,200,0.055)';
          } else {
            ctx.fillStyle = `rgba(120,168,255,${Math.min(a, 0.9)})`;
          }
          ctx.beginPath();
          ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    };

    const rings = (t) => {
      for (let i = ripples.length - 1; i >= 0; i -= 1) {
        const age = (t - ripples[i].t) / RIPPLE_MS;
        if (age >= 1) {
          ripples.splice(i, 1);
          continue;
        }
        const eased = 1 - (1 - age) ** 3;
        ctx.beginPath();
        ctx.arc(ripples[i].x, ripples[i].y, eased * 260, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(77,141,255,${0.3 * (1 - age)})`;
        ctx.lineWidth = 1.2;
        ctx.stroke();
      }
    };

    const frame = (t) => {
      ctx.clearRect(0, 0, w, h);
      aurora(t);
      lattice(t);
      rings(t);
      raf = requestAnimationFrame(frame);
    };

    const start = () => {
      if (raf == null) raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      if (raf != null) cancelAnimationFrame(raf);
      raf = null;
    };
    const onVisibility = () => (document.hidden ? stop() : start());

    resize();

    if (reduced) {
      /* Still paint once — the depth is worth keeping, the motion isn't. */
      ctx.clearRect(0, 0, w, h);
      aurora(0);
      lattice(0);
      window.addEventListener('resize', resize);
      return () => window.removeEventListener('resize', resize);
    }

    window.addEventListener('resize', resize);
    window.addEventListener('pointermove', onPointer, { passive: true });
    window.addEventListener('pointerdown', onClick, { passive: true });
    window.addEventListener('pointerleave', onLeave);
    window.addEventListener('scroll', readScroll, { passive: true, capture: true });
    document.addEventListener('visibilitychange', onVisibility);
    start();

    return () => {
      stop();
      window.removeEventListener('resize', resize);
      window.removeEventListener('pointermove', onPointer);
      window.removeEventListener('pointerdown', onClick);
      window.removeEventListener('pointerleave', onLeave);
      window.removeEventListener('scroll', readScroll, { capture: true });
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className="fixed inset-0 z-0 pointer-events-none"
    />
  );
}
