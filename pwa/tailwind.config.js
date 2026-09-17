/** Design tokens — trading-terminal visual language.
 *  Layered surfaces (not one flat card colour), hairline borders, and colour
 *  that only ever carries meaning: green money, red risk, amber guardian,
 *  cyan machine. */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  /* Built from template literals at runtime (`wash-${tone}`), so the content
     scanner can't see them. */
  safelist: ['wash-green', 'wash-red', 'wash-ai'],
  theme: {
    extend: {
      colors: {
        // --- surfaces, darkest to lightest ---
        paper: '#0A0A0C',        // app background
        card: '#121216',         // default raised surface
        'card-2': '#17171C',     // nested / hovered surface
        well: '#0E0E11',         // sunken surface (inputs, tracks, tables)
        // --- ink ---
        ink: '#F4F4F6',
        'ink-2': '#9E9EA8',
        muted: '#63636E',
        // --- lines ---
        line: '#232329',
        'line-soft': '#1A1A1F',
        'line-strong': '#32323A',
        // --- semantic ---
        green: '#00D98B',
        'green-soft': 'rgba(0, 217, 139, 0.12)',
        'green-glow': 'rgba(0, 217, 139, 0.28)',
        red: '#FF4D5E',
        'red-soft': 'rgba(255, 77, 94, 0.12)',
        'red-glow': 'rgba(255, 77, 94, 0.28)',
        amber: '#FFB020',
        'amber-soft': 'rgba(255, 176, 32, 0.12)',
        ai: '#4D8DFF',
        'ai-soft': 'rgba(77, 141, 255, 0.12)',
        'ai-glow': 'rgba(77, 141, 255, 0.3)',
        // --- categorical accents (charts, tags, per-domain identity) ---
        violet: '#A78BFA',
        'violet-soft': 'rgba(167, 139, 250, 0.12)',
        cyan: '#22D3EE',
        'cyan-soft': 'rgba(34, 211, 238, 0.12)',
        teal: '#2DD4BF',
        'teal-soft': 'rgba(45, 212, 191, 0.12)',
        orange: '#FB923C',
        'orange-soft': 'rgba(251, 146, 60, 0.12)',
        pink: '#F472B6',
        'pink-soft': 'rgba(244, 114, 182, 0.12)',
      },
      fontFamily: {
        disp: ['"Outfit"', 'system-ui', 'sans-serif'],
        body: ['"Inter"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      /* Fluid type. Every step is clamp(mobile, slope, desktop) interpolating
         between a 375px phone and a 1440px monitor, because the previous fixed
         px scale was drawn for the phone and then looked microscopic on a
         desktop the app also has to run on. One scale, both devices, no
         breakpoint-specific font classes scattered through the screens. */
      fontSize: {
        hero: ['clamp(42px, 2.066vw + 34.25px, 64px)', { lineHeight: '1', letterSpacing: '-0.03em' }],
        lock: ['clamp(30px, 0.939vw + 26.48px, 40px)', { lineHeight: '1.05', letterSpacing: '-0.02em' }],
        contract: ['clamp(20px, 0.469vw + 18.24px, 25px)', { lineHeight: '1.15', letterSpacing: '-0.01em' }],
        brand: ['clamp(16px, 0.235vw + 15.12px, 18.5px)', { lineHeight: '1.2', letterSpacing: '-0.01em' }],
        btn: ['clamp(14px, 0.094vw + 13.65px, 15px)', { lineHeight: '1.2' }],
        body: ['clamp(13.5px, 0.188vw + 12.8px, 15.5px)', { lineHeight: '1.55' }],
        sec: ['clamp(12px, 0.141vw + 11.47px, 13.5px)', { lineHeight: '1.5' }],
        chip: ['clamp(10.5px, 0.094vw + 10.15px, 11.5px)', { lineHeight: '1.2', letterSpacing: '0.02em' }],
        eyebrow: ['clamp(10px, 0.141vw + 9.47px, 11.5px)', { lineHeight: '1.2', letterSpacing: '0.13em' }],

        /* Plain fluid sizes with no baked-in tracking, for the places that
           used a literal `text-[11px]`. Same 375→1440 interpolation. */
        f9: ['clamp(9px, 0.141vw + 8.47px, 10.5px)', { lineHeight: '1.35' }],
        f10: ['clamp(10px, 0.141vw + 9.47px, 11.5px)', { lineHeight: '1.4' }],
        f11: ['clamp(11px, 0.141vw + 10.47px, 12.5px)', { lineHeight: '1.45' }],
        f13: ['clamp(13px, 0.188vw + 12.3px, 15px)', { lineHeight: '1.5' }],
        f15: ['clamp(15px, 0.235vw + 14.12px, 17.5px)', { lineHeight: '1.4' }],

        /* Tailwind's own steps, made fluid, so a stray `text-xs` on a 27"
           monitor stops rendering at phone size. */
        xs: ['clamp(12px, 0.141vw + 11.47px, 13.5px)', { lineHeight: '1.45' }],
        sm: ['clamp(13.5px, 0.188vw + 12.8px, 15.5px)', { lineHeight: '1.5' }],
        base: ['clamp(15px, 0.235vw + 14.12px, 17.5px)', { lineHeight: '1.55' }],
      },
      borderRadius: { card: '16px', block: '10px', pill: '999px' },
      /* Layout rhythm breathes with the viewport for the same reason. */
      spacing: {
        gutter: 'clamp(14px, 0.94vw + 10.5px, 24px)',
        cardpad: 'clamp(15px, 0.47vw + 13.2px, 20px)',
        cardgap: 'clamp(12px, 0.38vw + 10.6px, 16px)',
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -12px rgba(0,0,0,0.5)',
        lift: '0 2px 4px rgba(0,0,0,0.4), 0 16px 40px -16px rgba(0,0,0,0.7)',
        'glow-ai': '0 0 0 1px rgba(77,141,255,0.35), 0 0 24px -4px rgba(77,141,255,0.25)',
        'glow-green': '0 0 0 1px rgba(0,217,139,0.35), 0 0 24px -4px rgba(0,217,139,0.25)',
        'glow-red': '0 0 0 1px rgba(255,77,94,0.35), 0 0 24px -4px rgba(255,77,94,0.25)',
        inset: 'inset 0 1px 2px rgba(0,0,0,0.5)',
      },
    },
  },
  plugins: [],
}
