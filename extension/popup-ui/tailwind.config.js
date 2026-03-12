/** @type {import('tailwindcss').Config} */
export default {
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                bgBase: '#0a0a0a',
                bgSurface: 'rgba(23, 23, 23, 0.6)',
                bgHover: 'rgba(38, 38, 38, 0.8)',
                borderColor: 'rgba(255, 255, 255, 0.08)',
                borderHighlight: 'rgba(255, 255, 255, 0.15)',
                textMain: '#f8fafc',
                textMuted: '#94a3b8',
                accent: '#3b82f6',
                accentHover: '#60a5fa',
                danger: '#ef4444',
                dangerHover: '#f87171',
                success: '#10b981',
            }
        },
    },
    plugins: [],
}
