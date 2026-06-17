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

	const lenis = new Lenis({ autoRaf: false, lerp: 0.12 });
	lenis.on('scroll', ScrollTrigger.update);
	gsap.ticker.add((t) => lenis.raf(t * 1000));
	gsap.ticker.lagSmoothing(0);

	document.fonts.ready.then(() => {
		const heroSplit = SplitText.create('.hero h1', { type: 'lines', mask: 'lines' });
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
