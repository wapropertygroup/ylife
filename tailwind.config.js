/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',

  // Scan all templates and JS files across all Tailwind-powered apps.
  // yhome uses hand-written CSS and is intentionally excluded.
  content: [
    './ystocker/templates/**/*.html',
    './yplanner/templates/**/*.html',
    './yplanter/templates/**/*.html',
    './ytracker/templates/**/*.html',
    './ypay/templates/**/*.html',
    './yimage/templates/**/*.html',
    './ybg/templates/**/*.html',
    // Static JS (i18n, Alpine component scripts)
    './ystocker/static/**/*.js',
    './yplanner/static/**/*.js',
    './yplanter/static/**/*.js',
    './ytracker/static/**/*.js',
    './ypay/static/**/*.js',
    './yimage/static/**/*.js',
    './ybg/static/**/*.js',
    // Python routes can contain CSS class strings passed to templates
    './*/routes.py',
  ],

  theme: {
    extend: {
      colors: {
        // Each app declares --brand, --brand-dark, --brand-light in its style.css.
        // The <alpha-value> placeholder lets opacity modifiers like bg-brand/20 work.
        brand: {
          DEFAULT: 'rgb(var(--brand) / <alpha-value>)',
          dark:    'rgb(var(--brand-dark) / <alpha-value>)',
          light:   'rgb(var(--brand-light) / <alpha-value>)',
        },
        surface: '#0f172a',
        panel:   '#1e293b',
        border:  '#334155',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
    },
  },
}
