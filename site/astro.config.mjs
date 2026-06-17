// @ts-check
import { defineConfig, fontProviders } from 'astro/config';

import starlight from '@astrojs/starlight';

import tailwindcss from '@tailwindcss/vite';

// https://astro.build/config
export default defineConfig({
	site: 'https://angelsen.github.io',
	base: '/repld',

	prefetch: {
		prefetchAll: true,
		defaultStrategy: 'viewport',
	},

	fonts: [
		{
			provider: fontProviders.fontsource(),
			name: 'Geist Mono',
			cssVariable: '--font-geist-mono',
			weights: [400, 500, 700],
		},
		{
			provider: fontProviders.fontsource(),
			name: 'Bricolage Grotesque',
			cssVariable: '--font-bricolage',
			weights: [500, 700, 800],
		},
	],

	integrations: [
		starlight({
			title: 'repld',
			customCss: ['./src/styles/starlight-theme.css'],
			expressiveCode: {
				themes: ['vesper'],
				useStarlightUiThemeColors: true,
				styleOverrides: {
					borderRadius: '8px',
					frames: {
						editorBackground: '#0d1117',
						terminalBackground: '#0d1117',
					},
				},
			},
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/angelsen/repld' },
			],
			sidebar: [
				{
					label: 'Start here',
					items: [
						{ slug: 'docs' },
						{ slug: 'docs/guides/getting-started' },
						{ slug: 'docs/guides/browser' },
						{ slug: 'docs/guides/gists' },
					],
				},
				{
					label: 'Reference',
					items: [{ autogenerate: { directory: 'docs/reference' } }],
				},
			],
		}),
	],

	vite: {
		plugins: [tailwindcss()],
	},
});
