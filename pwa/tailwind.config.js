/** Design tokens are the spec's, verbatim. docs/design_spec.md §1. */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#09090B',
        card: '#18181B',
        ink: '#FAFAFA',
        'ink-2': '#A1A1AA',
        muted: '#71717A',
        line: '#27272A',
        'line-soft': '#18181B',
        green: '#10B981',
        'green-soft': 'rgba(16, 185, 129, 0.15)',
        red: '#EF4444',
        'red-soft': 'rgba(239, 68, 68, 0.15)',
        amber: '#F59E0B',
        'amber-soft': 'rgba(245, 158, 11, 0.15)',
        ai: '#3B82F6',
        'ai-soft': 'rgba(59, 130, 246, 0.15)',
      },
      fontFamily: {
        disp: ['"Outfit"', 'system-ui', 'sans-serif'],
        body: ['"Inter"', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
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
