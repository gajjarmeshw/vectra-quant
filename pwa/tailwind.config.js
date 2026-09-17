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
      /* Every colour resolves through a CSS variable defined in theme.css, so
         one `data-theme` attribute on <html> re-skins the whole app. Channels
         are stored bare (`R G B`) specifically so the `<alpha-value>` slot
         keeps working — `border-line/70` is used all over the screens. */
      colors: Object.fromEntries(
        [
          'paper', 'card', 'card-2', 'well',
          'ink', 'ink-2', 'muted',
          'line', 'line-soft', 'line-strong',
          'green', 'red', 'amber', 'ai',
          'violet', 'cyan', 'teal', 'orange', 'pink',
          'green-soft', 'red-soft', 'amber-soft', 'ai-soft',
          'violet-soft', 'cyan-soft', 'teal-soft', 'orange-soft', 'pink-soft',
        ].map((name) => [name, `rgb(var(--c-${name}) / <alpha-value>)`]),
      ),
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
        card: 'var(--shadow-card)',
        lift: 'var(--shadow-lift)',
        inset: 'var(--shadow-inset)',
        'glow-ai': '0 0 0 1px rgb(var(--c-ai) / 0.35), 0 0 24px -4px rgb(var(--c-ai) / 0.25)',
        'glow-green': '0 0 0 1px rgb(var(--c-green) / 0.35), 0 0 24px -4px rgb(var(--c-green) / 0.25)',
        'glow-red': '0 0 0 1px rgb(var(--c-red) / 0.35), 0 0 24px -4px rgb(var(--c-red) / 0.25)',
      },
    },
  },
  plugins: [],
}
