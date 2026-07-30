# Token Narrative Protocol

This protocol defines how Solana Radar fills token information, chooses one primary narrative, and explains the context shown in the dashboard.

Important boundary: this is a caught-token enrichment protocol, not a narrative discovery protocol. It is allowed to start from market/onchain data only because Solana Radar first catches tokens through scanner alerts. Do not use this file for open-ended narrative research. For narrative scans, use the top-level universal source-first router first.

## Goal

Every caught token must have one clear primary narrative. Secondary themes can be shown as flavor, but they must not create duplicate narrative buckets for the same token.

The dashboard should answer:

- what the scanner caught;
- why this token belongs to this narrative;
- whether the narrative is project-driven, news-driven, social-driven, or only inferred;
- what changed since the first signal;
- what the noticed wallets are sitting on now.

## Token Scope

Narratives are assigned only to tokens with scanner alerts. The universe list is market discovery; it should not create narrative cards by itself.

## Lane Scope

`reactivation` is the only production lane:

- age: `30d+`;
- market cap: any positive value up to `$5m`;
- liquidity: `>= $3k`;
- market prefilter: at least `$100` reported 1h volume, ranked by 5m burst plus 1h activity;
- scan allocation: explicit capacity for `ignition`, `early`, `established`, and `mature` market-cap stages;
- alert requirement: distributed net buying, buyer concentration limits, and current balances that retain the acquired supply.

`micro_sticky`, `cheap_sticky`, `breakout`, `incubation`, and `young` are disabled. Historical alerts from those lanes must not appear in the production dashboard or consume Reactivation monitoring capacity.

## Scanner Filter Categorizer

Narrative category answers what the token story is. Scanner filter category answers which radar caught it.

Every caught token should expose:

- `primaryFilter`: `reactivation`;
- `reactivation_stage`: `ignition`, `early`, `established`, or `mature`;
- filter criteria shown in the dashboard.

The production dashboard accepts alerts whose backend `lane` is explicitly `reactivation`. It must not relabel an old alert from a disabled lane as Reactivation merely because the token later fits the same age or market-cap range. Never infer a filter from token name or narrative.

## Enrichment Timing

When a scanner alert is created, token intel must be fetched in the same scan before the alert is written. This is separate from the social scan limit: if X/social enrichment is skipped because the per-scan social budget is full, backend token intel and narrative classification should still run. The alert record should include GMGN metadata, Dexscreener profile links, Bright Data/public context when available, social results when available, caller graph when available, and the backend narrative decision.

The dashboard should read the backend `token_intel.narrative` as the source of truth. The frontend must not infer a narrative from keywords. If backend enrichment is missing, the dashboard should show `Token intel missing` and treat the token as not classified until enrichment is refreshed.

## Source Priority

Use the strongest available source first. Lower-priority sources can support a claim, but should not override higher-priority evidence.

Split source priority into two different jobs:

- Factual token fields can start from market/onchain data: CA, pair, age, liquidity, volume, ATH, wallet flow.
- Narrative thesis should prefer official project sources, source posts, public context, and matched social evidence over ticker/name/market metadata. Market data can show that a token is active; it cannot prove what narrative is spreading.

1. Direct market/onchain data:
   - GMGN token, ATH, and K-line endpoints;
   - Dexscreener token and pair profile;
   - GeckoTerminal pool and OHLCV data;
   - Helius parsed transactions;
   - local scanner alert history.
2. Official project sources:
   - project website;
   - official X account;
   - official Telegram, Discord, docs, GitHub, or app;
   - token profile links embedded in GMGN, Dexscreener, or CoinGecko.
3. Third-party project coverage:
   - CoinGecko categories and listing data;
   - DEXTools/news writeups;
   - hackathon, fund, launchpad, exchange, or ecosystem announcements.
4. Social/news context:
   - Bright Data X/public-web results;
   - direct public X posts;
   - mainstream or niche news sources relevant to the narrative.
5. Inference:
   - ticker/name keywords;
   - metadata wording;
   - visual branding.

Inference cannot override clear project evidence. Example: `BLOXX / BloxAPI` is `Gaming/Creator Infra`, not `Classic Meme`, even if the ticker looks meme-like.

Caller/account biographies are not token lore. They can be shown in caller metrics, but they must not classify the token narrative unless the account is confirmed as an official project source from token profile links. Narrative classification should use the matched post text, official token/project metadata, and public context, not generic bio wording from an influencer or ecosystem account.

Official X profiles are project evidence when the X URL comes from GMGN, Dexscreener, or another official token profile link. The profile bio/description should be fetched during token-intel enrichment and treated above generic public context. Example: `Lambda / LBD` should use the official `@Lambdaprivacy` description about zero-knowledge, MPC, and confidential execution as the core thesis, not a generic `DevTool/Infra` sentence.

## Required Token Fields

Each token deep dive should show these fields when available:

- `symbol`
- `name`
- `mint/token address`
- `primary pair`
- `dex`
- `project website`
- `official X / Telegram / Discord`
- `token age`
- `launch date/time`
- `current market cap`
- `current liquidity`
- `24h and 1h volume`
- `GMGN ATH market cap`
- `GMGN ATH date/time`
- `GMGN ATH source`
- `GMGN ATH status`
- `Caught mcap`: market cap observed by the scanner when the token first produced a signal
- `Market now`: latest scanner snapshot market cap and liquidity
- `Market phase`: current scanner market cap as a percentage of GMGN ATH
- `Launch context`: compact early market path and current entry-risk note when a verified retrospective read exists
- `first signal date/time`
- `profit since first signal`
- `noted wallet median PnL`
- `noted wallet best PnL`
- `unique noticed flow in SOL`
- `unique buy/event count`
- `duplicate raw event rows collapsed`
- `wallet classes`
- `common funder clusters`
- `common recipient/routed-buy clusters`
- `social status`
- `scanner filter`
- `scanner filter criteria`
- `caller graph`
- `caller follower counts`
- `caller post engagement`: views, likes, reposts, replies, quotes, bookmarks
- `caller engagement rate`
- `primary narrative`
- `secondary flavor`
- `why this narrative`
- `news/project overlay`
- `source links`

## Token Detail Display Order

Every caught-token deep dive should use the same visible order:

1. Header: symbol, name.
2. Hero metrics: profit since caught, unique noticed flow.
3. `Token age`: age first, then launch date/time.
4. `Caught`: first signal date/time, then first observed market cap, formatted as `date / $mcap mcap`.
5. `ATH mcap`: ATH date/time, then ATH market cap, formatted as `date / $mcap mcap`, with source chip or status if missing.
6. `Market now`: latest scanner-observed market cap, liquidity, and snapshot time.
7. `Market phase`: `Near ATH`, `Upper range`, `Mid-range`, or `Low range` based on current mcap / GMGN ATH.
8. `Launch context`: optional compact retrospective read, only when backed by verified chart/history data.
9. `Scanner filter`: filter/lane chips, inferred marker if applicable, and criteria for the primary filter.
10. `Signal quality`: max score, unique buys, unique flow, unique wallets, and duplicate raw rows collapsed.
11. `Wallet cluster`: wallet-class mix, common funder, common recipient, and routed-buy markers.
12. `Wallet PnL`: unique wallets, median PnL, best PnL.
13. `Primary narrative`: primary narrative chip first, then secondary flavor chips.
14. `Lore thesis`: one normalized investable thesis, not separate `Why`, `News overlay`, and `Lore evidence` rows.
15. `Proof basis`: compact evidence chips such as official profile, project/public context, social posts, and social heat.
16. `Source links`, token address, GMGN token terminal link.
17. Deep sections: caller network, noticed-wallet PnL, signal timeline.

Market phase is not the same as scanner filter. The live `Reactivation` filter scans old migrated tokens from the first tradeable low-cap stage instead of waiting for `$100k` or a trusted ATH. GMGN ATH remains mandatory dashboard context and affects conviction: a deep correction can become `Actionable`, while a strong low-cap wave with high velocity or unresolved ATH is shown as `Hot Reactivation`. `Upper range` and `Near ATH` must remain visible risk context rather than silently excluding the token from discovery.

Do not add duplicate rows for the same concept. In particular:

- Do not show separate `Secondary flavor`, `Why`, `News overlay`, `Lore evidence`, and `Source hygiene` rows in the token detail. Collapse narrative explanation into `Primary narrative`, `Lore thesis`, and `Proof basis`.
- Do not show separate `First OBS mcap`, `Latest OBS mcap`, `First/current price`, or generic `Market` rows in the token detail. Collapse them into `Caught`, `ATH mcap`, and `Market now`.
- `Caught` and `ATH mcap` must use the same date-first formatting: `date / $mcap mcap`.
- Signal totals in token cards, narrative cards, and filter cards must use deduped event signatures, not summed alert rows. Raw alert rows belong only in `Signal Timeline`.

## ATH Rules

Every caught token must show a GMGN ATH metric:

- `GMGN ATH mcap`
- `GMGN ATH date/time`
- `GMGN ATH source`
- `GMGN ATH status`: `ready`, `market cap ready/date pending`, `pending`, `retry pending`, or `missing API key`.

This is a required token-card and token-detail field. It should not be hidden behind fallback values.

Historical ATH must be labeled by source quality:

- `GMGN ATH`: preferred when available.
- `OHLCV high`: acceptable fallback from GeckoTerminal chart data.
- `Caught mcap`: first-alert scanner snapshot only; this is not a historical ATH and must not be rendered inside the ATH metric.
- `Market now`: latest scanner snapshot only; this is not a historical ATH and must not be rendered inside the ATH metric.

ATH enrichment is mandatory for caught tokens:

- Every token that appears in a new alert must get an ATH fetch attempt in the same scan.
- Recent caught tokens must stay in a priority ATH queue, controlled by `ath_recent_alert_limit`.
- `ath_max_tokens_per_scan` applies only to broad market/universe backfill, not to caught-token ATH enrichment.
- If the ATH provider rate-limits or fails, store `ath_error` and `ath_error_checked_at`, then retry after `ath_error_cache_ttl_minutes`.
- Dashboard should collapse scanner mcap display into `Caught` and `Market now`, not separate first/latest OBS rows.
- The UI labels for scanner snapshots should be `Caught` and `Market now`, not `ATH` or any high-watermark wording.
- Signal Timeline rows should include each alert's `OBS mcap`, so the token keeps a readable log of the market cap at every scanner catch.

Low-liquidity or stale alternate pools must not define ATH unless explicitly labeled as a low-liquidity anomaly. For normal dashboard display, prefer GMGN token ATH and use the primary liquid pool only for market-now validation.

## Social Status Rules

The social chip must distinguish these states:

- `social not checked`: alert was created before social enrichment existed or no social snapshot was written.
- `social disabled: missing bright data api key`: scanner could not call Bright Data.
- `social no mentions`: Bright Data ran and found zero relevant public results.
- `social quiet`: one or two relevant public results.
- `social warming`: at least the configured warming threshold.
- `social hot`: high count and multiple authors; this can be broad hype rather than a clean early signal.

Do not interpret `not checked` or `disabled` as "no one is talking about it."

## Caller Graph Rules

Caller graph is built from public X posts that mention the token or its contract.

Pipeline:

1. Bright Data Discover finds relevant public X URLs.
2. Direct X status URLs are passed to Bright Data X Scraper.
3. X Scraper enriches posts with author and post metrics.
4. Posts are grouped by caller account.

Caller aggregation must dedupe by normalized post URL or post ID. Repeated snapshots of the same X post must not increase post count, views, likes, reposts, replies, quotes, bookmarks, or engagement totals. Keep the strongest/latest follower count and the highest-scoring top post.

Caller fields:

- `author`
- `display name`
- `followers`
- `following`
- `is verified`
- `posts mentioning token`
- `views`
- `likes`
- `reposts`
- `replies`
- `quotes`
- `bookmarks`
- `engagements`
- `engagement rate by views`
- `engagement rate by followers`
- `top post URL`

Caller graph status:

- `enriched`: at least one X post URL was scraped and has metrics.
- `discover_only`: mentions were found, but no post-level metrics were available.
- `unavailable`: Bright Data was disabled or no relevant X URLs were found.

Profile-only X results can count as mention evidence, but they should not pretend to have engagement metrics.

## Narrative Scoring

Create candidate narratives from project evidence, news, socials, and metadata. Assign one primary narrative by weighted evidence:

- `+7`: official/project evidence strongly defines the category.
- `+6`: current external news cycle is the main driver.
- `+5`: major ecosystem catalyst, fund, hackathon, listing, or launchpad event.
- `+4`: clear product/category terms in website, profile, or third-party listing.
- `+3`: repeated social/KOL evidence for a category.
- `+2`: ticker/name/branding keyword only.

Tie-break order:

1. official project/product category;
2. hard catalyst or funding/listing/hackathon;
3. current news cycle;
4. social trend;
5. ticker/name keyword;
6. generic meme packaging.

## Primary Narrative

The primary narrative is the top scored category. The dashboard should show exactly one primary narrative per token.

Tilt labels:

- `strong tilt`: score is at least 7 and the lead over the second category is at least 2.
- `medium tilt`: score is at least 4.
- `weak tilt`: only soft evidence exists.
- `unclear`: no category has enough evidence.

## Lore Layer

Every classified token must also have a `lore` object. The primary narrative is the bucket; lore is the investable story inside that bucket.

Required lore fields:

- `headline`: short name of the actual story, not just the category.
- `summary`: one-paragraph thesis explaining why this story is the driver.
- `driver`: one of `project/product catalyst`, `external news cycle`, `social-lore catalyst`, `mascot/community meme`, `source-backed narrative`, or `not classified`.
- `confidence`: `high`, `medium`, or `low`.
- `evidence`: source rows with kind, claim, source, URL when available, and social metrics when available.
- `conflicts`: weaker or conflicting signals, such as mascot wrapper vs stronger social lore.
- `source_hygiene`: filtering notes, especially for short tickers and false-positive social results.

Lore source order:

1. official profile and official links;
2. project website metadata;
3. third-party writeups/listings;
4. direct X posts with mint/full-name match and metrics;
5. weaker social/profile-only mentions.

Short ticker rule: for symbols with three characters or fewer, ticker-only matches are not enough. The source must include the mint address, full token name, or an official project link. Example: `$BP` alone can refer to Backpack, so it must not enter Barking Puppy caller graph or lore evidence without stronger matching.

The lore layer can override mascot/category packaging. Example: a dog-branded token can still be primary `Classic Meme` if the stronger story is Roaring Kitty / psyop / buyback-burn lore.

## Calibration Workflow

When a narrative looks questionable, use one token as the calibration case before scaling the rule:

1. Build a source pack from official profile, official links, third-party writeups, direct X posts, and caller metrics.
2. Identify false positives, especially short ticker collisions.
3. Decide the primary narrative, secondary flavor, lore headline, and confidence manually from evidence.
4. Convert that decision into a general rule, not a one-off UI edit.
5. Reclassify the calibration token.
6. Backfill the same rule across existing alerts.
7. Verify the dashboard shows backend `token_intel.narrative.lore`, not frontend inference.

## Secondary Flavor

Secondary flavor is allowed when a non-primary category has meaningful but weaker evidence.

Examples:

- `HANTA`: primary `Health/Bio`, secondary `Anime/Asia`.
- `BLOXX`: primary `Gaming/Creator Infra`, no `Classic Meme` secondary unless there is real meme-driven distribution.

Secondary flavor should never create a duplicate narrative card.

## Narrative Categories

Current allowed categories:

- `Health/Bio`
- `Gaming/Creator Infra`
- `DevTool/Infra`
- `AI`
- `Finance/DeFi`
- `Politics/Prediction`
- `Sports`
- `Animals`
- `Anime/Asia`
- `Classic Meme`
- `Unclear`

New categories can be added only when at least two tokens or one high-confidence project clearly requires the bucket.

`Privacy/Compute Infra` is used for project evidence centered on confidential computing, confidential execution, zero-knowledge, MPC, secure enclaves, or encrypted/private compute. It is a project/product category, not a meme wrapper.

## News Or Project Overlay

Each token should have one overlay:

- `News overlay`: used when external current events are likely part of the pump driver.
- `Project overlay`: used when the token is tied to a real product, hackathon, funding event, app, launchpad, or ecosystem milestone.
- `No strong external overlay`: used only when no reliable current context is found.

Overlay text must include:

- the core driver;
- whether it is confirmed or inferred;
- why the primary narrative wins;
- source links.

## Manual Override Rules

Manual correction is allowed when source evidence beats keyword classification.

When overriding:

- record the source;
- explain what changed;
- keep the primary narrative singular;
- do not hide conflicting secondary flavor;
- do not rewrite market/onchain facts unless verified.

## Current Examples

### HANTA

- Primary: `Health/Bio`
- Tilt: `strong tilt`
- Secondary: `Anime/Asia`
- Reason: Hanta/hantavirus keyword aligns with current hantavirus news cycle; anime/Kun branding is packaging.
- Overlay type: `News overlay`
- ATH source: GMGN ATH.

### BLOXX

- Primary: `Gaming/Creator Infra`
- Tilt: `strong tilt`
- Secondary: none by default.
- Reason: BloxAPI is a Game Creator Launchpad / Roblox-style creator infrastructure project with AI tooling, analytics, Studio plugin, and token-gated product features.
- Overlay type: `Project overlay`
- Supporting context: reported Pump.fun Build in Public Hackathon winner with $250k investment.
- ATH source: GMGN ATH.

### BP / Barking Puppy

- Primary: `Classic Meme`
- Tilt: `strong tilt`
- Secondary: `Animals`
- Reason: puppy/dog branding is the wrapper, but public context and social lore point to Roaring Kitty / Kevin14 / TSUKI / buyback-burn / psyop speculation as the stronger market narrative.
- Overlay type: `Narrative overlay`
- Social hygiene: short ticker `$BP` is not enough evidence by itself; social and public-context matches must include the Barking Puppy name, mint address, or official project links to avoid unrelated Backpack `$BP` contamination.

## Dashboard Behavior

Main page:

- show caught tokens only;
- show primary narrative chip;
- show secondary flavor chip only if relevant;
- show token age, current mcap, liquidity, first signal, ATH, social status, and PnL since first signal.

Narratives page:

- group by primary narrative only;
- allow drill-down into narrative detail;
- show tokens inside the narrative;
- allow click-through from narrative token row to token deep dive.

Token deep dive:

- show full project/news overlay;
- show sources;
- show ATH and source;
- show first signal and profit since first signal;
- show caller network with followers and post engagement;
- show noticed wallet PnL and classes.
