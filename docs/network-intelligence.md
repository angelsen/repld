# Network Intelligence — targeted outreach via persistent data pipeline

## Concept

Build a graph of people and companies from multiple data sources, enriched
continuously by the kernel. Use the graph to answer: **who should I reach,
and what content should I produce to reach them?**

Same fundamental loop as SEO (observe signals → produce aligned content →
measure → iterate), but at individual resolution instead of keyword clusters,
with feedback in hours instead of weeks.

## Data sources

### Company fundamentals (API — no scraping)

| Source | Data | Access |
|--------|------|--------|
| **brreg.no** | Registration, address, industry code (NACE), legal form, status | Free REST API, no registration. `data.brreg.no/enhetsregisteret/api/` |
| **proff.no** | Revenue, employees, board members, owners, shareholders, sector | CDP gist (API is paid; web UI shows the data publicly). |
| **SSB** | Aggregate business stats, employment by region/industry, demographics | PxWebApi v2, no registration. `ssb.no/en/api` |

### Social signal (CDP gists)

| Source | Data | Gist |
|--------|------|------|
| **X (Twitter)** | Tweets, likes, retweets, followers, engagement patterns, real-time topics | `from x import X` (exists) |
| **LinkedIn** | Role, company, connections, professional content, endorsements | `from linkedin import LI` (to build) |
| **Instagram** | Visual content, engagement, brand presence | `from instagram import IG` (exists) |

### Geo targeting

brreg supports filtering by `kommunenummer` (municipality code) and
`postadresse`. Trondheim = kommune 5001. This enables: "show me all
companies in Trondheim in industry X with revenue > Y."

## Data model (SQLite)

```sql
CREATE TABLE persons (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    handle_x    TEXT,
    handle_li   TEXT,
    company     TEXT,
    role        TEXT,
    industry    TEXT,
    city        TEXT,
    followers   INTEGER,
    pagerank    REAL,
    updated_at  TIMESTAMP
);

CREATE TABLE companies (
    orgnr       TEXT PRIMARY KEY,  -- brreg org number
    name        TEXT,
    city        TEXT,
    nace        TEXT,              -- industry classification
    employees   INTEGER,
    revenue     INTEGER,
    founded     TEXT,
    updated_at  TIMESTAMP
);

CREATE TABLE edges (
    source_id   INTEGER,
    target_id   INTEGER,
    relation    TEXT,  -- follows, engages, works_at, owns, board_member
    weight      REAL,
    platform    TEXT,
    UNIQUE(source_id, target_id, relation)
);

CREATE TABLE engagement (
    person_id   INTEGER,
    content_id  TEXT,
    action      TEXT,  -- like, retweet, reply, comment
    platform    TEXT,
    timestamp   TIMESTAMP
);

CREATE TABLE content (
    id          TEXT PRIMARY KEY,
    author_id   INTEGER,
    text        TEXT,
    platform    TEXT,
    likes       INTEGER,
    views       INTEGER,
    topics      TEXT,  -- JSON array
    created_at  TIMESTAMP
);
```

## Pipeline

```
seed → enrich → analyze → act → measure

1. SEED: brreg API → companies in Trondheim, industry X, revenue > Y
2. ENRICH: proff → owners/board → persons table
         X → social profiles → engagement patterns
         LinkedIn → role, connections → professional graph
3. ANALYZE: pagerank, betweenness centrality, topic clustering
4. ACT: generate content brief → draft content → post
5. MEASURE: track engagement from targets → iterate
```

### Continuous enrichment via kernel primitives

```python
# Seed: pull companies from brreg
@every(3600)
async def seed_companies():
    new = await brreg.search(kommune="5001", nace="62")  # IT companies in Trondheim
    for c in new:
        db.upsert_company(c)
    return f"{len(new)} companies checked"

# Enrich: one person per tick, rate-limited
@every(120)
async def enrich_next():
    person = db.next_unenriched()
    if not person:
        return
    # Cross-reference across platforms
    if person.handle_x:
        tweets = await x.user_tweets(person.user_id_x, count=50)
        db.store_engagement(person.id, tweets)
    return f"enriched {person.name}"

# Analyze: recompute graph metrics hourly
@every(3600)
async def recompute():
    G = db.to_networkx()
    pr = nx.pagerank(G)
    bc = nx.betweenness_centrality(G)
    db.update_metrics(pr, bc)
    movers = db.top_movers(period="24h")
    if movers:
        return f"top movers: {', '.join(m.name for m in movers[:5])}"
```

## Target flows

### Company → People

```
brreg: "IT companies in Trondheim, >10 employees"
  → proff: board members, owners for each
    → LinkedIn: current role, connections
      → X: what they talk about, who they engage with
        → content brief: "write about Y to reach person Z"
```

### Person → Network

```
X: "who's talking about [your niche] in Norway"
  → profile each active voice
    → LinkedIn: where do they work, who's in their network
      → brreg: what companies are connected
        → expand: board member overlap, shared investors
```

### Content strategy

```
For target person X:
  1. Analyze their last 100 tweets/posts
  2. Extract: topics, tone, format preferences, peak hours
  3. Analyze what they like/retweet (not just post)
  4. Cross-reference with their professional context (role, industry challenges)
  5. Generate brief:
     "Write a [technical thread / short video / article] about [topic]
      with [contrarian / data-heavy / narrative] angle.
      Post [Tuesday 9-11am]. Include [concrete metrics].
      Avoid [listicles — they never engage with those]."
```

## Build order

1. **`graph.py` gist** — SQLite wrapper: `add_person()`, `add_company()`,
   `add_edge()`, `query()`, `to_networkx()`. Shared across all gists.
2. **`brreg.py` gist** — REST API client (no CDP needed): `search()`,
   `company()`, `roles()`. Seed the company pipeline. Free, no auth.
3. **`proff.py` gist** — CDP-based (API is paid, web UI is free):
   `search()`, `company()`, `board()`, `owners()`, `financials()`.
   Rate-limit gently — public site, no auth needed.
4. **X enrichment** — extend existing `x` gist usage: pull engagement
   data for known handles, store in graph DB.
5. **`linkedin.py` gist** — CDP-based (hardest): `.search()`, `.profile()`,
   `.connections()`. Heavy rate limiting, session management.
6. **Analysis queries** — pagerank, clustering, content briefs. Pure Python
   on the graph DB, driven by the agent.

## oversikt.ai integration

This pipeline is a natural extension of oversikt.ai — same architecture
(browser-as-auth, config-driven namespaces), same target market (Norwegian
business intelligence).

### Existing primitives in oversikt.ai

- **brreg namespace** — already live in the relay. Same data, different
  execution path (executeScript vs urllib).
- **proff.no** — listed as "Requested" in PRODUCT.md. The repld gist can
  port to a namespace config (tab strategy, __NEXT_DATA__ extraction).
- **Share system** — `POST /share`, `GET /s/{hash}` with PIN protection.
  Publish interactive analyses as shareable links.
- **Rich document renderer** — mermaid, maps, carousels. Can render
  network graphs inline.
- **Canvas** — headless Chrome rendering for social media screenshots/video.
- **`/visualize` skill** — composing data into visual output.

### blog.oversikt.ai / news.oversikt.ai

Each pipeline run produces a publishable analysis. The blog arm serves
four purposes simultaneously:

1. **Case study** — demonstrates oversikt.ai capabilities with real data.
2. **SEO** — captures "hvem eier [company]", "IT-selskaper Trondheim",
   "styremedlemmer [sector]" searches. Zero competition today.
3. **Lead gen** — the subjects of the analysis are the target customers.
   Board members find their own names, see the product in action.
4. **Product demo** — the analysis itself was produced by the product.
   "The email demonstrates the product in the act of selling it."

### Content types

- **Sector maps** — "Who controls health-IT in Midt-Norge" — interactive
  D3 force graph, every node clickable to company profile.
- **Board network analysis** — cross-company connectors, ownership chains,
  power structures. Updated daily via `@every`.
- **Political connections** — municipal board members + which companies
  they're connected to. Public data, public interest.
- **Hiring intelligence** — "the 50 fastest-growing tech companies in
  Trondheim" with leadership profiles and growth trajectories.
- **Digital twins** — living data models of companies, sectors, regions.
  Not a snapshot — continuously updated by the kernel.

### The SEO loop (same muscle as traditional SEO)

Traditional: analyze PAA/SERPs → write content shaped to rank → measure.
Network intelligence: analyze power structures → publish analysis that
names the people → they find it via vanity search → they see the product.

Same loop, individual-resolution targeting instead of keyword clusters,
social feedback in hours instead of weeks waiting for indexing.

## Positioning — not a SaaS, an operating layer

The conventional approach: build a vertical SaaS (one problem, one UI, one
database, one subscription). Every competitor is doing this.

The oversikt/repld approach: **the browser is the operating system, the
LLM is the assistant, the gist/namespace is the connector.** No new
database — query live from the source. No new UI — the AI host is the UI.
No new auth — the browser already has it.

Two layers of value:

### "Tell me what you need to know" (intelligence)

Read-only. Immediate value. Zero risk. This is the cold email opener.

- Ownership chain tracing (4 layers in 30 seconds)
- Board network mapping (who sits where, who connects what)
- Portfolio monitoring (financial trajectory, leadership changes)
- Competitive landscape (social signal, press mentions)
- Sector mapping ("all IT companies in Trondheim with >50 employees")

The pipeline runs continuously via `@every`. Not a report — a living model.

### "Tell me what you need to do" (action)

Operates systems the user is already logged into. The hard part that
"AI agent" companies can't do: reliable browser automation across auth,
iframes, CSRF, session management.

- Filing forms (Altinn, Skatteetaten)
- Sending messages (email, LinkedIn outreach)
- Updating records (CRM, accounting)
- Monitoring + acting (watch for changes, trigger workflows)

Trust model: `tab.confirm()` gates destructive actions. Copilot, not
autopilot. Human stays in the loop for what matters.

### The edge

Everyone can call an API. Making an agent reliably operate a web app
designed for humans requires deep understanding of Chrome internals,
CDP, extension architecture, Chromium source-level quirks. That
infrastructure knowledge is the moat — not the product surface.

The positioning: "You don't need a new SaaS. You need someone who can
make your existing systems talk to each other — and act on what they
find."

### oversikt.ai = playground + portfolio + product

The same system serves three purposes simultaneously:

- **Playground** — prototype gists, reverse-engineer APIs, build
  pipelines. The Viking Venture brief was built here in one session.
- **Portfolio** — shareable documents, blog posts, live demos. The
  proof that this isn't theoretical.
- **Product** — "it's already built, let me configure it for your
  use case." When the demo lands, the conversation moves to scope.

Every gist written, every pipeline prototyped, every analysis run
serves all three. Nothing is wasted. Work done to learn becomes work
shown to sell becomes work charged for.

## Go-to-market

### Cold outreach

Don't send links — nobody clicks links from strangers. Put the
intelligence IN the message. The data IS the pitch. Example:

> Erik — Viking Growth har 13+ porteføljeselskaper på tvers av Norge,
> Sverige, Danmark og Finland. Flowbox, WorkPoint, JAMIX, CheckProof
> — disse er usynlige i Brønnøysundregistrene. Vi fant dem på 60
> sekunder ved å krysse brreg, proff.no og LinkedIn programmatisk.
>
> Du har 56 styreseter. Vi kan gi deg kontinuerlig oversikt over hele
> porteføljen — finansielle endringer, lederskifter, konkurransebilde
> — automatisk, ikke kvartalsvis.
>
> Kan jeg vise deg det live? 15 minutter.

He reads it and thinks "how do they know this?" — that's the hook.
The live demo is the close: share screen, run the pipeline, 60 seconds.

### Why PE firms first

1. **They already pay for this.** €100k+ per deal on consultants doing
   due diligence, market mapping, portfolio monitoring manually.
2. **They understand leverage.** "30 seconds vs 3 days" is their language.
3. **They can fund you.** Demo → service contract (€5-10k/mo) →
   venture conversation. Two paths from one meeting.
4. **Small market.** 20-30 Nordic PE/VC firms. Need 2-3 conversations,
   not scale.
5. **LinkedIn is the channel.** Norwegian PE leaders aren't on X.
   LinkedIn gist is the unlock for outreach.

### Pricing — analysis products

| Tier | Scope | Price |
|------|-------|-------|
| Company profile | Single target (e.g. Signicat) — ownership chain, board, financials, social signal | 5-15k NOK |
| Sector map | Region × industry (e.g. "IT in Trondheim") — 100+ companies screened, board network, connectors | 25-50k NOK |
| Portfolio intelligence | Full fund brief (e.g. Viking Venture) — all portfolio companies, cross-border, financial health | 50-100k NOK |

Marginal cost to produce: near zero. The pipeline runs in seconds.
The value is weeks of analyst time replaced.

### The pitch

"I'm a developer who understands browser internals, Chrome extension
architecture, and how web apps actually work under the hood. I mapped
your entire portfolio structure — 13 companies across 4 countries,
ownership chains, financials, board networks — in 60 seconds.

You don't need a dashboard project. You don't need a 6-month
integration. You need someone who understands how these systems work
and can wire them together in an afternoon. That's what I do."

### Outreach flow

No cold links. The intelligence is in the message. The shared document
is accessed via a code on your own domain. The meeting is self-service.

**LinkedIn DM template:**

> Erik — Viking Growth har 13+ porteføljeselskaper på tvers av 4 land.
> Flowbox, WorkPoint, JAMIX — usynlige i norske registre. Vi fant dem
> på 60 sekunder.
>
> Jeg har laget en interaktiv porteføljeoversikt. Gå til oversikt.ai,
> trykk "Åpne delt dokument" og skriv inn kode VG-2026.
>
> Vil du se det live? Tar 15 minutter — book direkte her: oversikt.ai/meet
>
> Fredrik Angelsen — oversikt.ai

Three paths: browse the document, book a meeting, or ignore.

**Share code mechanic (`oversikt.ai` + "Åpne delt dokument"):**

- Feels exclusive — a personal code, not a public URL
- Safe — they navigate to oversikt.ai themselves, no sketchy link
- Memorable — "VG-2026" sticks
- Tracks engagement — you know when they opened it
- Product demo — they're using oversikt.ai to view the brief

**Meeting link (`oversikt.ai/meet`):**

- Reverse meeting — they view your calendar and book you
- No back-and-forth scheduling
- Core oversikt.ai utility — solve your own friction, it becomes a feature

### Build philosophy

Every friction point solved for yourself becomes a product feature.
Meeting booking, shared documents, intelligence briefs — built by
using the product obsessively, not from a feature roadmap. The product
grows out of real workflow, not hypothetical user stories.

### First move

Target: Viking Venture / Viking Growth, Trondheim.
Contact: Erik Fjellvær Hagen (Managing Partner) or Jostein Vik (Partner).
Channel: LinkedIn DM.
Brief: built. Demo: ready. Meet link: wire up. One message starts it.

## Constraints

- **Rate limiting is our responsibility.** LinkedIn especially — slow crawl,
  respect limits, rotate sessions. brreg/proff APIs have their own limits.
- **SQLite for persistence.** Stdlib, survives kernel restarts, no deps.
  DuckDB if analytics queries become the bottleneck.
- **Privacy.** All data sourced from public APIs/profiles. No scraping
  private data. The graph is built from what people chose to make public.
- **Incremental.** The graph grows over days/weeks. Don't try to crawl
  everything at once — the kernel runs 24/7, let it accumulate.
