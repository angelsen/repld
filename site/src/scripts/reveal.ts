// Shared scroll-reveal choreography for editorial pages.
// Lenis smooth scroll synced to GSAP ScrollTrigger; masked line reveals on
// headings (fonts-gated); staggered fades on section bodies.
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { SplitText } from 'gsap/SplitText';
import Lenis from 'lenis';

export function initReveal() {
	gsap.registerPlugin(ScrollTrigger, SplitText);
	const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
	if (reduced) return;

	// Gate heading reveals on just the heading font, not every font. Read the real
	// hashed family off a heading so it survives Astro's per-build font hashing.
	const headingFontReady = (): Promise<unknown> => {
		const el = document.querySelector('.hero h1, [data-anim] h2');
		if (!el) return document.fonts.ready;
		const cs = getComputedStyle(el);
		const family = cs.fontFamily.split(',')[0].trim();
		return document.fonts.load(`${cs.fontWeight || '800'} 1em ${family}`).catch(() => {});
	};

	const lenis = new Lenis({ autoRaf: false, lerp: 0.12 });
	lenis.on('scroll', ScrollTrigger.update);
	gsap.ticker.add((t) => lenis.raf(t * 1000));
	gsap.ticker.lagSmoothing(0);

	headingFontReady().then(() => {
		const heroSplit = SplitText.create('.hero h1', { type: 'lines', mask: 'lines' });
		gsap.set('.hero h1', { visibility: 'visible' }); // lines start masked offscreen — no flash
		gsap.from(heroSplit.lines, {
			yPercent: 115,
			duration: 0.9,
			stagger: 0.09,
			ease: 'power4.out',
			onComplete: () => heroSplit.revert(),
		});
		gsap.from('.tag', { y: 16, autoAlpha: 0, duration: 0.5, ease: 'power3.out' });
		gsap.from('.hero-sub', {
			y: 22,
			autoAlpha: 0,
			duration: 0.7,
			ease: 'power3.out',
			delay: 0.3,
		});

		document.querySelectorAll<HTMLElement>('[data-anim] h2').forEach((el) => {
			const s = SplitText.create(el, { type: 'lines', mask: 'lines' });
			gsap.set(el, { visibility: 'visible' });
			gsap.from(s.lines, {
				yPercent: 115,
				duration: 0.85,
				stagger: 0.09,
				ease: 'power4.out',
				scrollTrigger: { trigger: el, start: 'top 82%' },
				onComplete: () => s.revert(),
			});
		});
	});

	document.querySelectorAll<HTMLElement>('[data-anim]').forEach((sect) => {
		const els = sect.querySelectorAll('[data-reveal], .body-text, .section-label, .narrow');
		if (!els.length) return;
		gsap.from(els, {
			y: 26,
			autoAlpha: 0,
			duration: 0.7,
			stagger: 0.1,
			ease: 'power3.out',
			scrollTrigger: { trigger: sect, start: 'top 72%' },
		});
	});
}
