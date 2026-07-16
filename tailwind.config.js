/** @type {import('tailwindcss').Config} */
// Tokens lifted from the approved design mockup (Gerlyn-Redesign-standalone.html,
// screen 1a "Design-system foundation"). brand-500 stays for identity (logo mark,
// large accents); action-600 is one shade darker so white text on it passes WCAG AA.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          500: '#2E9E6B',
        },
        action: {
          50: '#EAF6F0',
          200: '#9CCDB4',
          600: '#1E7A4F',
          700: '#14603C',
        },
        ink: {
          900: '#17251E',
          600: '#56655D',
          500: '#3E4E45',
          400: '#8A968F',
        },
        surface: {
          bg: '#F5F7F6',
          line: '#E3E9E5',
          linelight: '#EFF3F0',
        },
        status: {
          ok: { bg: '#E3F3EB', text: '#14603C', dot: '#1E7A4F' },
          warn: { bg: '#FCF0D4', text: '#7C4A05', dot: '#B45309' },
          danger: { bg: '#FDE8E6', text: '#9A1C12', dot: '#B42318' },
        },
      },
      borderRadius: {
        input: '8px',
        card: '12px',
      },
      fontSize: {
        metric: ['32px', { fontWeight: '700', letterSpacing: '-0.01em' }],
      },
      boxShadow: {
        card: '0 1px 3px rgba(23,37,30,.05)',
      },
    },
  },
  plugins: [],
};
