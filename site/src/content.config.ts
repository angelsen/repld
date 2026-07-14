import { glob } from 'astro/loaders';
import { defineCollection, z } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
	docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
	blog: defineCollection({
		loader: glob({ pattern: '**/*.md', base: './src/content/blog' }),
		schema: z.object({
			title: z.string(),
			pubDate: z.coerce.date(),
			description: z.string(),
			tags: z.array(z.string()).default([]),
			model: z.string().optional(),
		}),
	}),
};
