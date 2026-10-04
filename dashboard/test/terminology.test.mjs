import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {JSDOM} from "jsdom";
import {TERMS, termForLabel, tooltipPosition, installTerminology} from "../terminology.js";

function fixture(t) {
  const dom = new JSDOM(`<body><main id="content"><div class="review-table-head"><span>Position left</span></div>
    <button class="review-row"><span class="chip">Watch</span></button>
    <aside class="token-detail"><h2>ATH coin</h2><div class="detail-metric"><span>Caught mcap</span><strong>$50k</strong></div>
    <div class="section-heading"><h3>Original position</h3></div><div class="evidence-facts"><div><span>Retained supply</span><strong>0%</strong></div></div>
    <span class="chip">freshish 2</span><details><summary>Market context &amp; ATH</summary></details>
    <div class="kv"><span><a href="#source">Retained supply</a></span></div></aside></main><button id="outside">Outside</button></body>`);
  const doc = dom.window.document;
  const controller = installTerminology(doc);
  controller.refresh(doc.querySelector("#content"));
  t.after(() => { controller.destroy(); dom.window.close(); });
  const button = doc.querySelector('[data-term="position"]');
  const popup = doc.querySelector('[role="tooltip"]');
  const pointer = (target, type, pointerType = "mouse", relatedTarget = null) => {
    const event = new dom.window.MouseEvent(type, {bubbles:true, relatedTarget});
    Object.defineProperty(event, "pointerType", {value:pointerType});
    target.dispatchEvent(event);
  };
  return {dom, doc, controller, button, popup, pointer};
}

test("terms separate position retention, supply, attribution and provider labels", () => {
  assert.equal(termForLabel("Position left"), "position");
  assert.equal(termForLabel("Retained supply"), "retained_supply");
  assert.equal(termForLabel("Signal cohort"), "top_cohort");
  assert.equal(termForLabel("Wallets still holding"), "holders");
  assert.equal(termForLabel("Retention checks"), "retention_checks");
  assert.equal(termForLabel("GMGN wallet labels"), "gmgn_labels");
  assert.equal(termForLabel("Observed position movements"), "position_activity");
  assert.match(TERMS.position_activity.text, /Полная история кошельков не проверена/);
  assert.match(TERMS.position_activity.note, /не являются доказательством продажи/);
  assert.match(TERMS.position.text, /не 75% всего суплая/);
  assert.match(TERMS.top_cohort.note, /не означает, что группа всё продала/);
  assert.match(TERMS.coordination.note, /не доказательство/);
  assert.match(TERMS.gmgn_labels.note, /не доказательство/);
  assert.match(TERMS.partial.note, /не ноль/);
});
test("both networks and dynamic class / time labels resolve, unrelated text does not", () => {
  for (const [label, expected] of [["Current FDV","fdv"], ["ATH price / GMGN","ath_price"], ["Solana Tracker ATH","ath"],
    ["freshish 12","freshish"], ["low_tx 1","low_tx"], ["Checked 01 Oct, 03:08","checked"], ["Common funder Abc...","funder"]]) {
    assert.equal(termForLabel(label), expected, label);
  }
  for (const label of ["Random token", "20 wallets", "0%", "None", null]) assert.equal(termForLabel(label), null);
  for (const value of Object.values(TERMS)) assert.ok(value.title && value.text && value.note);
});
test("placement flips above and clamps to all viewport edges", () => {
  assert.deepEqual(tooltipPosition({left:10, top:20, bottom:40}, {width:340,height:150}, {width:393,height:852}), {left:12,top:48,width:340});
  assert.deepEqual(tooltipPosition({left:370, top:700, bottom:730}, {width:340,height:150}, {width:393,height:852}), {left:41,top:542,width:340});
  assert.deepEqual(tooltipPosition({left:-100, top:40, bottom:55}, {width:340,height:276}, {width:320,height:300}), {left:12,top:12,width:296});
});
test("enhancement is scoped and idempotent; no nested controls or replaced token identities", t => {
  const {doc, controller} = fixture(t);
  assert.equal(doc.querySelectorAll(".term-trigger").length, 5);
  controller.refresh(doc.querySelector("#content"));
  assert.equal(doc.querySelectorAll(".term-trigger").length, 5);
  assert.equal(doc.querySelectorAll("button button, a button, summary button").length, 0);
  assert.equal(doc.querySelector("h2").textContent, "ATH coin");
  assert.equal(doc.querySelector(".evidence-facts strong").textContent, "0%");
  assert.equal(doc.querySelector(".term-trigger").type, "button");
});
test("desktop hover opens; moving into the explanation keeps it open", t => {
  const {dom, button, popup, pointer} = fixture(t);
  const original = dom.window.setTimeout;
  const scheduled = [];
  dom.window.setTimeout = fn => { scheduled.push(fn); return scheduled.length; };
  t.after(() => { dom.window.setTimeout = original; });
  pointer(button, "pointerover");
  assert.equal(popup.hidden, false);
  assert.equal(popup.lang, "ru");
  assert.equal(button.getAttribute("aria-describedby"), popup.id);
  assert.match(popup.textContent, /три четверти/);
  pointer(button, "pointerout", "mouse", popup);
  pointer(popup, "pointerover");
  assert.equal(scheduled.length, 0);
  pointer(popup, "pointerout", "mouse", dom.window.document.body);
  assert.equal(scheduled.length, 1);
  scheduled[0]();
  assert.equal(popup.hidden, true);
});
test("keyboard focus opens, Escape dismisses without losing focus or prior descriptions", t => {
  const {dom, doc, button, popup} = fixture(t);
  button.setAttribute("aria-describedby", "other-description");
  button.focus();
  assert.equal(popup.hidden, false);
  assert.equal(button.getAttribute("aria-describedby"), `other-description ${popup.id}`);
  doc.dispatchEvent(new dom.window.KeyboardEvent("keydown", {key:"Escape",bubbles:true}));
  assert.equal(popup.hidden, true);
  assert.equal(doc.activeElement, button);
  assert.equal(button.getAttribute("aria-describedby"), "other-description");
});
test("leaving the hovered trigger cannot dismiss a keyboard-focused explanation", t => {
  const {dom, doc, button, popup, pointer} = fixture(t);
  let scheduled = 0;
  dom.window.setTimeout = () => { scheduled += 1; return scheduled; };
  button.focus();
  pointer(button, "pointerout", "mouse", doc.body);
  assert.equal(scheduled, 0);
  assert.equal(popup.hidden, false);
  doc.querySelector("#outside").focus();
  assert.equal(popup.hidden, true);
});
test("mobile ignores hover, tap pins, second tap and outside click dismiss", t => {
  const {doc, button, popup, pointer} = fixture(t);
  pointer(button, "pointerover", "touch");
  assert.equal(popup.hidden, true);
  button.click();
  assert.equal(popup.hidden, false);
  pointer(button, "pointerout", "touch", doc.body);
  assert.equal(popup.hidden, false);
  button.click();
  assert.equal(popup.hidden, true);
  button.click(); doc.querySelector("#outside").click();
  assert.equal(popup.hidden, true);
});
test("focus leaving a pinned trigger dismisses; scroll and resize also dismiss", t => {
  const {dom, doc, button, popup} = fixture(t);
  button.focus(); button.click(); doc.querySelector("#outside").focus();
  assert.equal(popup.hidden, true);
  button.click(); doc.querySelector("#content").dispatchEvent(new dom.window.Event("scroll"));
  assert.equal(popup.hidden, true);
  button.click(); dom.window.dispatchEvent(new dom.window.Event("resize"));
  assert.equal(popup.hidden, true);
});
test("card rerender discards old explanation and enhances new labels with one popup", t => {
  const {doc, controller, button, popup} = fixture(t);
  button.click();
  const content = doc.querySelector("#content");
  content.innerHTML = '<aside class="token-detail"><div class="detail-metric"><span>Current FDV</span></div></aside>';
  controller.refresh(content);
  assert.equal(popup.hidden, true);
  assert.equal(doc.querySelectorAll('[role="tooltip"]').length, 1);
  doc.querySelector('[data-term="fdv"]').click();
  assert.match(popup.textContent, /Полностью разводнённая/);
});
test("untrusted label content is text, not injected tooltip HTML", t => {
  const {doc, controller, popup} = fixture(t);
  const content = doc.querySelector("#content");
  content.innerHTML = '<aside class="token-detail"><div class="kv"><span></span></div></aside>';
  const label = content.querySelector("span");
  label.textContent = 'Common funder <img src=x onerror=alert(1)>';
  controller.refresh(content);
  assert.equal(label.querySelector("img"), null);
  label.querySelector("button").click();
  assert.equal(popup.querySelector("img"), null);
});
test("shared glossary and versioned stylesheet are wired into both network renderers", () => {
  for (const file of ["app.js","robinhood.js"]) {
    const source = readFileSync(new URL(`../${file}`,import.meta.url), "utf8");
    assert.match(source, /installTerminology/);
    assert.match(source, /terminology\.refresh\(/);
    assert.match(source, /terminology\.dismiss\(/);
  }
  const html = readFileSync(new URL("../index.html",import.meta.url), "utf8");
  assert.match(html, /terminology\.css\?v=20261004-wallet-activity-v4/);
});
