/** Design tokens are the spec's, verbatim. docs/design_spec.md §1. */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#FEFEFD',
        card: '#FFFFFF',
        ink: '#0B0B0A',
        'ink-2': '#4A4A46',
        muted: '#8A8A85',
        line: '#E9E9E6',
        'line-soft': '#F2F2EF',
        green: '#0E9F6E',
        'green-soft': '#E6F6F0',
        red: '#E5484D',
        'red-soft': '#FDEBEC',
        amber: '#D97706',
        'amber-soft': '#FBF1E2',
        ai: '#2447F5',
        'ai-soft': '#EDF0FE',
      },
      fontFamily: {
        disp: ['"Bricolage Grotesque Variable"', 'system-ui', 'sans-serif'],
        body: ['"Geist Sans"', 'system-ui', 'sans-serif'],
        mono: ['"Geist Mono"', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        hero: ['54px', { lineHeight: '1', letterSpacing: '-0.02em' }],
        lock: ['26px', { lineHeight: '1.1' }],
        contract: ['22px', { lineHeight: '1.2' }],
        brand: ['17px', { lineHeight: '1.2' }],
        btn: ['15px', { lineHeight: '1.2' }],
        body: ['13.5px', { lineHeight: '1.45' }],
        sec: ['12px', { lineHeight: '1.4' }],
        chip: ['11px', { lineHeight: '1.2' }],
        eyebrow: ['10px', { lineHeight: '1.2', letterSpacing: '0.12em' }],
      },
      borderRadius: { card: '20px', block: '14px', pill: '999px' },
      spacing: { gutter: '18px', cardpad: '20px', cardgap: '14px' },
    },
  },
  plugins: [],
}
