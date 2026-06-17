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
			sidebar: [
				{ label: 'Guides', items: [{ autogenerate: { directory: 'docs/guides' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'docs/reference' } }] },
			],
		}),
	],

	vite: {
		plugins: [tailwindcss()],
	},
});
