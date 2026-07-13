ROOT := $(shell git rev-parse --show-toplevel)

.PHONY: deploy site-build site-preview

deploy: site-build
	@git branch -D gh-pages >/dev/null 2>&1 || true
	@tmpdir=$$(mktemp -d)/gh-pages && \
	git worktree add --orphan -b gh-pages "$$tmpdir" && \
	cd "$$tmpdir" && \
	cp -r $(ROOT)/site/dist/. . && \
	touch .nojekyll && \
	git add -A && \
	git commit -m "deploy site" && \
	git push origin gh-pages --force && \
	cd $(ROOT) && \
	git worktree remove "$$tmpdir" && \
	echo "✓ deployed to https://angelsen.github.io/repld/"

site-build:
	cd site && pnpm build

site-preview:
	cd site && pnpm preview
