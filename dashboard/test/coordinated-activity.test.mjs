import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {resolveCoordinatedActivity, renderCoordinatedActivity} from "../coordinated-activity.js";

const now = Date.parse("2026-10-01T12:00:00Z");
const options = {now};
const solana = "So11111111111111111111111111111111111111112";
const evm = "0x1234567890abcdef1234567890abcdef12345678";
const evidence = (extra = {}) => ({
  version:1, status:"pattern", checked_at:"2026-10-01T11:50:00Z",
  ownership:"not_established", bundle:"not_established", scope:"Selected buyer subset",
  metrics:{held_supply_pct:50, material_pattern:true, material_union_held_supply_pct:3.25, buyer_count:4}, coverage:{checked:4, eligible:9},
  signals:[{code:"timing", label:"Buy timing", family:"timing", detail:"Buys clustered in the checked window",
    members:[solana], wallet_count:4, held_supply_pct:3.25, supporting_only:true}],
  limitations:["Unresolved buyers excluded"], ...extra,
});

test("Solana resolves thesis first, otherwise the latest alert without mutating input", () => {
  const older = evidence({status:"coordination_watch"});
  const newer = evidence();
  const token = {alerts:[{created_at:"2026-10-01T11:55:00Z", coordinated_activity:newer},
    {created_at:"2026-10-01T10:00:00Z", coordinated_activity:older}]};
  const before = structuredClone(token);
  assert.equal(resolveCoordinatedActivity(token), newer);
  assert.deepEqual(token, before);
  token.signalThesis = {coordinated_activity:older};
  assert.equal(resolveCoordinatedActivity(token), older);
  token.signalThesis.coordinated_activity = {status:"not_checked"};
  assert.deepEqual(resolveCoordinatedActivity(token), {status:"not_checked"});
});

test("alert fallback uses window timestamps and stable array order when dates are missing", () => {
  const older = evidence(), newer = evidence({status:"coordination_watch"});
  assert.equal(resolveCoordinatedActivity({alerts:[{window_start:"2026-10-01T10:00:00Z", coordinated_activity:older},
    {window_end:"2026-10-01T11:00:00Z", coordinated_activity:newer}]}), newer);
  assert.equal(resolveCoordinatedActivity({alerts:[{coordinated_activity:older}, {coordinated_activity:newer}]}), newer);
  assert.equal(resolveCoordinatedActivity({alerts:"bad"}), null);
});

test("Robinhood reads raw token evidence, not the Solana alert fallback", () => {
  const raw = evidence();
  assert.equal(resolveCoordinatedActivity({coordinated_activity:raw, signalThesis:{coordinated_activity:{status:"not_checked"}}}, "robinhood"), raw);
  assert.equal(resolveCoordinatedActivity({alerts:[{coordinated_activity:raw}]}, "robinhood"), null);
});

test("supporting coincidences stay out of lists and unrelated held supply is not attributed", () => {
  const weak = evidence({metrics:{held_supply_pct:50, material_pattern:false, buyer_count:4}});
  assert.equal(renderCoordinatedActivity(weak, {...options, compact:true}), "");
  assert.match(renderCoordinatedActivity(weak, options), /Supporting coincidence/);
  const material = renderCoordinatedActivity(evidence(), {...options, compact:true});
  assert.match(material, /3\.25% supply/);
  assert.doesNotMatch(material, /50\.00%/);
});

test("structured detector details remain visible and escaped", () => {
  const html = renderCoordinatedActivity(evidence({signals:[{label:"Funding", detail:{funding_span_seconds:40, source:'<script>x</script>'}}]}), options);
  assert.match(html, /funding span seconds: 40/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("pattern and funding watch use warning-only badges and bounded evidence", () => {
  const html = renderCoordinatedActivity(evidence(), options);
  assert.match(html, /class="chip warn"/);
  assert.match(html, />Coordinated pattern</);
  assert.match(html, /3\.25% held supply at check \/ 4 buyers \/ Checked 10m ago/);
  assert.match(html, /Buy timing.*supporting only/);
  assert.match(html, /Scope: Selected buyer subset \/ Coverage: checked: 4 \/ eligible: 9/);
  assert.match(html, /Unresolved buyers excluded/);
  assert.match(renderCoordinatedActivity(evidence({status:"coordination_watch"}), options), />Funding-linked pattern</);
});

test("all provider text is escaped, including coverage, limitations, and signal fields", () => {
  const attack = '<img src=x onerror="run()"> & \'text\'';
  const html = renderCoordinatedActivity(evidence({scope:attack, coverage:{'<b>':attack}, limitations:[attack],
    signals:[{label:attack, detail:attack, members:[attack], wallet_count:attack, held_supply_pct:attack}]}), options);
  assert.doesNotMatch(html, /<img|<b>|<script|href="(?:javascript|data):/);
  assert.match(html, /&lt;img src=x onerror=&quot;run\(\)&quot;&gt; &amp; &#39;text&#39;/);
  assert.match(html, /&lt;b&gt;/);
  assert.match(renderCoordinatedActivity({status:"not_checked", detail:attack}, options), /&lt;img/);
  assert.match(renderCoordinatedActivity(evidence({signals:[{family:attack, code:attack}]}), options), /&lt;img/);
  assert.match(renderCoordinatedActivity(evidence({signals:[{code:attack}]}), options), /&lt;img/);
});

test("zero supply and buyer count stay distinct from missing, empty, boolean, or invalid values", () => {
  const zero = renderCoordinatedActivity(evidence({metrics:{material_union_held_supply_pct:0, buyer_count:0}, signals:[]}), options);
  assert.match(zero, /0\.00% held supply at check \/ 0 buyers/);
  for (const value of [null, undefined, "", " ", false, true, NaN, Infinity, -1, "bad"]) {
    const html = renderCoordinatedActivity(evidence({metrics:{material_union_held_supply_pct:value, buyer_count:value}, signals:[]}), options);
    assert.match(html, /\u2014 held supply at check \/ \u2014 buyers/);
    assert.doesNotMatch(html, /0\.00%|\/ 0 buyers/);
  }
  assert.match(renderCoordinatedActivity(evidence({metrics:{material_union_held_supply_pct:0.000001, buyer_count:1}}), options), /&lt;0\.01% held supply/);
  assert.match(renderCoordinatedActivity(evidence({metrics:{material_union_held_supply_pct:101, buyer_count:0.5}}), options), /\u2014 held supply at check \/ \u2014 buyers/);
});

test("absent per-signal metrics are not inferred from members or token totals", () => {
  const html = renderCoordinatedActivity(evidence({signals:[{label:"Timing", members:[solana]}]}), options);
  assert.match(html, /\(\u2014 wallets \/ \u2014 supply at check\)/);
  assert.doesNotMatch(html, /\(1 wallets/);
});

test("shared infrastructure is not upgraded to ownership, exact bundling, Ready, or an accusation", () => {
  const html = renderCoordinatedActivity(evidence({ownership:"established", bundle:"established",
    signals:[{code:"router", family:"router", label:"Shared service markers", detail:"CEX / LI.FI / Relay routes", supporting_only:true}]}), options);
  assert.match(html, /Common control \/ exact bundle not established/);
  assert.match(html, /Shared CEX \/ router \/ LI\.FI \/ Relay markers are not ownership evidence/);
  assert.match(html, /supporting only/);
  assert.doesNotMatch(html, /chip good|Ready|common owner confirmed|bundle confirmed|Proxima|Clean|safe to buy/i);
});

test("stale checks remain historical and are visibly marked in detail and compact rows", () => {
  const result = evidence({checked_at:"2026-10-01T09:00:00Z"});
  const html = renderCoordinatedActivity(result, options);
  assert.match(html, /Previous check 3h ago/);
  assert.match(html, /3\.25% held supply at check/);
  assert.doesNotMatch(html, /current holding|current supply|fresh|chip good/i);
  assert.match(renderCoordinatedActivity(result, {...options, compact:true}), /supply \/ previous/);
  assert.match(renderCoordinatedActivity(evidence({checked_at:"2026-10-01T10:30:00Z"}), options), /Previous check 1h ago/);
});

test("missing, invalid, and future check times are unverified, not checked now", () => {
  for (const checked_at of [undefined, null, "", "bad", "2026-10-01T13:00:00Z"]) {
    const result = evidence({checked_at});
    assert.match(renderCoordinatedActivity(result, options), /Check age \u2014 \/ time unverified/);
    assert.match(renderCoordinatedActivity(result, {...options, compact:true}), /supply \/ age \u2014/);
  }
  assert.match(renderCoordinatedActivity(evidence()), /time unverified/);
});

test("not checked and unknown statuses are neutral in detail and absent from lists", () => {
  for (const status of ["not_checked", "pending", "checked", "PATTERN", undefined, '<script>']) {
    const result = evidence({status, detail:"Coverage unavailable"});
    assert.equal(renderCoordinatedActivity(result, {...options, compact:true}), "");
    const html = renderCoordinatedActivity(result, options);
    assert.match(html, status === "not_checked" ? /Coordinated activity not checked/ : /status unverified/);
    assert.match(html, /Coverage unavailable/);
    assert.doesNotMatch(html, /chip warn|chip good|3\.25%|Buy timing|<script>/);
  }
});

test("no pattern is only a checked-subset observation, never a green all-clear", () => {
  const result = evidence({status:"no_pattern_in_checked_subset"});
  const html = renderCoordinatedActivity(result, options);
  assert.match(html, /No pattern in checked subset/);
  assert.match(html, /Common control \/ exact bundle not established/);
  assert.doesNotMatch(html, /chip good|Ready|Clean|no coordination|3\.25%/i);
  assert.equal(renderCoordinatedActivity(result, {...options, compact:true}), "");
});

test("address links use validated 32-byte Solana and EVM addresses on the correct network only", () => {
  const result = evidence({signals:[{label:"Members", members:[solana, evm, "javascript:alert(1)", '" onmouseover="bad', "z".repeat(44)]}]});
  const html = renderCoordinatedActivity(result, options);
  assert.match(html, new RegExp(`href="https://solscan.io/account/${solana}"`));
  assert.equal((html.match(/<a /g) || []).length, 1);
  const rh = renderCoordinatedActivity(evidence({signals:[{members:[evm, solana, {address:evm, chain_id:1}]}]}), {...options, network:"robinhood"});
  assert.match(rh, new RegExp(`href="https://robinhoodchain.blockscout.com/address/${evm}"`));
  assert.equal((rh.match(/<a /g) || []).length, 1);
  assert.doesNotMatch(rh, /solscan/);
  assert.match(rh, /rel="noopener noreferrer"/);
  const mismatch = renderCoordinatedActivity(evidence({signals:[{members:[{address:solana, network:"robinhood"}, {address:evm, network:"solana"}]}]}), options);
  assert.doesNotMatch(mismatch, /<a /);
  assert.doesNotMatch(renderCoordinatedActivity(result, {...options, network:"unknown"}), /<a /);
});

test("detail output is bounded to three signals, three members each, and two limitations", () => {
  const result = evidence({signals:Array.from({length:100}, (_, i) => ({label:`Signal ${i}`, detail:"x".repeat(10000),
    members:Array(100).fill(solana)})), limitations:["First", "Second", "Third"]});
  const before = structuredClone(result);
  const html = renderCoordinatedActivity(result, options);
  assert.equal((html.match(/<strong>Signal /g) || []).length, 3);
  assert.equal((html.match(/<a /g) || []).length, 9);
  assert.doesNotMatch(html, /Signal 3|Third|<section|<table|<details/);
  assert.ok(html.length < 5000);
  assert.deepEqual(result, before);
  assert.equal(html, renderCoordinatedActivity(result, options));
});

test("compact rows contain only one warning badge, supply, and a freshness caveat", () => {
  const html = renderCoordinatedActivity(evidence(), {...options, compact:true});
  assert.equal((html.match(/<span/g) || []).length, 1);
  assert.match(html, />Coordinated pattern \/ 3\.25% supply<\/span>$/);
  assert.match(html, /title="Checked 10m ago/);
  assert.doesNotMatch(html, /Buy timing|<a |<div|<p|<section|<table/);
  assert.ok(html.length < 300);
  assert.equal(renderCoordinatedActivity(null, options), "");
  assert.equal(renderCoordinatedActivity([], options), "");
});

test("summary-only payload needs no members or scanner inputs to render evidence", () => {
  const result = evidence({signals:[{code:"funding", label:"Funding coincidence", detail:"Shared payer in checked subset", wallet_count:4,
    held_supply_pct:3.25, supporting_only:true}]});
  const html = renderCoordinatedActivity(result, options);
  assert.match(html, /Funding coincidence/);
  assert.match(html, /4 wallets \/ 3\.25% supply at check/);
  assert.doesNotMatch(html, /<a /);
  assert.match(renderCoordinatedActivity({status:"not_checked", limitations:["Funding history unavailable"]}, options), /Funding history unavailable/);
});

test("legacy Supply link labels stay neutral and both active renderers integrate existing tabs", () => {
  const app = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const rh = readFileSync(new URL("../robinhood.js", import.meta.url), "utf8");
  const bootstrap = readFileSync(new URL("../radar-bootstrap.js", import.meta.url), "utf8");
  assert.doesNotMatch(app, /Confirmed by independent evidence|No independent wallet links/);
  assert.match(app, /chip\("Converging link signals", "warn"\)/);
  assert.match(app, /chip\("No links found in checked subset"\)/);
  assert.match(app, /chip\("Common control not established"\)/);
  assert.match(app, /renderCoordinatedActivity\(resolveCoordinatedActivity\(token\), \{now: Date\.now\(\), compact: true\}\)/);
  assert.match(app, /function renderWalletsTab\(token\)[\s\S]*?renderCoordinatedActivity\(resolveCoordinatedActivity\(token\)/);
  assert.match(app, /function renderSupplyTab\(token\)[\s\S]*?renderCoordinatedActivity\(coordinated/);
  assert.match(bootstrap, /await import\("\.\/robinhood\.js\?/);
  assert.match(rh, /function wallets\(t\)[\s\S]*?renderCoordinatedActivity\(t\.coordinated_activity/);
  assert.match(rh, /function supply\(t\)[\s\S]*?renderCoordinatedActivity\(t\.coordinated_activity/);
});
