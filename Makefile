ROOT := $(shell git rev-parse --show-toplevel)

.PHONY: deploy site-build site-preview

deploy: site-build
	@tmpdir=$$(mktemp -d)/gh-pages && \
	git worktree add --detach "$$tmpdir" && \
	cd "$$tmpdir" && \
	git checkout --orphan gh-pages && \
	git rm -rf . >/dev/null 2>&1 || true && \
	cp -r $(ROOT)/site/dist/. . && \
	touch .nojekyll && \
	git add -A && \
	git commit -m "deploy site" && \
	git push origin gh-pages --force && \
	cd $(ROOT) && \
	git worktree remove "$$tmpdir" && \
	git checkout master && \
	echo "✓ deployed to https://angelsen.github.io/repld/"

site-build:
	cd site && pnpm build

site-preview:
	cd site && pnpm preview
