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

    /* Canvas takes resolved colour strings, not var() — so the theme tokens
       are read off the document once here and re-read whenever the theme
       attribute flips. */
    let blobHues = ['77 141 255', '167 139 250'];
    let palette = { dot: '120 168 255', idle: '160 168 200', idleA: 0.055, blobA: 0.055, ringA: 0.3 };
    const readPalette = () => {
      const cs = getComputedStyle(document.documentElement);
      const g = (k, fallback) => cs.getPropertyValue(k).trim() || fallback;
      palette = {
        dot: g('--amb-dot', '120 168 255'),
        idle: g('--amb-dot-idle', '160 168 200'),
        idleA: parseFloat(g('--amb-dot-idle-a', '0.055')),
        blobA: parseFloat(g('--amb-blob-a', '0.055')),
        ringA: parseFloat(g('--amb-ring-a', '0.3')),
      };
      /* Blue + teal rather than blue + violet: on the petrol base the violet
         read as a separate colour sitting on top, while teal belongs to the
         same family and lets the background recede behind the data. */
      blobHues = [g('--c-ai', '74 160 255'), g('--c-teal', '45 212 191')];
    };

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
        { x: 0.22 + 0.10 * Math.sin(t / 17000), y: 0.18 + 0.08 * Math.cos(t / 23000), c: blobHues[0], r: 0.62 },
        { x: 0.82 + 0.09 * Math.cos(t / 19000), y: 0.74 + 0.10 * Math.sin(t / 13000), c: blobHues[1], r: 0.55 },
      ];
      for (const b of blobs) {
        const cx = b.x * w;
        const cy = b.y * h;
        const rad = b.r * Math.max(w, h);
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad);
        g.addColorStop(0, `rgb(${b.c} / ${palette.blobA})`);
        g.addColorStop(0.5, `rgb(${b.c} / ${palette.blobA * 0.33})`);
        g.addColorStop(1, `rgb(${b.c} / 0)`);
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

          let a = palette.idleA + 0.02 * breathe + near * 0.42;
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

          if (a <= palette.idleA + 0.002) {
            ctx.fillStyle = `rgb(${palette.idle} / ${palette.idleA})`;
          } else {
            ctx.fillStyle = `rgb(${palette.dot} / ${Math.min(a, 0.9)})`;
          }
          ctx.beginPath();
          ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    };

    const rings = (t) => {
      for (let i = ripples.length - 1; i >= 0; i -= 1) {
        // Clamped at 0: ripples are stamped with performance.now() when the
        // pointer event fires, but `t` here is the frame-START timestamp, which
        // can be EARLIER than an event that arrived mid-frame. That made `age`
        // slightly negative, so the easing curve went negative and arc() threw
        // IndexSizeError on a negative radius -- killing the animation loop.
        const age = Math.max(0, (t - ripples[i].t) / RIPPLE_MS);
        if (age >= 1) {
          ripples.splice(i, 1);
          continue;
        }
        const eased = 1 - (1 - age) ** 3;
        ctx.beginPath();
        ctx.arc(ripples[i].x, ripples[i].y, eased * 260, 0, Math.PI * 2);
        ctx.strokeStyle = `rgb(${palette.dot} / ${palette.ringA * (1 - age)})`;
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
    readPalette();

    /* Repaint against the new tokens the moment the theme attribute changes. */
    const themeObserver = new MutationObserver(() => readPalette());
    themeObserver.observe(document.documentElement, { attributeFilter: ['data-theme'] });

    if (reduced) {
      /* Still paint once — the depth is worth keeping, the motion isn't. */
      ctx.clearRect(0, 0, w, h);
      aurora(0);
      lattice(0);
      window.addEventListener('resize', resize);
      const repaint = () => {
        readPalette();
        ctx.clearRect(0, 0, w, h);
        aurora(0);
        lattice(0);
      };
      const staticThemeObserver = new MutationObserver(repaint);
      staticThemeObserver.observe(document.documentElement, { attributeFilter: ['data-theme'] });
      return () => {
        window.removeEventListener('resize', resize);
        staticThemeObserver.disconnect();
        themeObserver.disconnect();
      };
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
      themeObserver.disconnect();
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
