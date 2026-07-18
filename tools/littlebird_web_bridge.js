#!/usr/bin/env node
/* Littlebird WEB bridge — reads meeting notes/summaries/transcripts + daily journals straight
 * from the Littlebird web client's IndexedDB (the `littlebird` DB), headless via a persistent
 * logged-in CDP profile. No desktop app, no IT. Mirrors the Zoom web bridge. Replaces the
 * native desktop LevelDB scrape (core/littlebird_local) so the Littlebird app can be uninstalled.
 *
 * Session: ~/.chrome-cdp-profile-littlebird, debug port 18821 (email-OTP login once; persists).
 *
 *   node tools/littlebird_web_bridge.js            # -> digests
 *   node tools/littlebird_web_bridge.js --json      # full JSON to stdout (for the python tool)
 *
 * Outputs:
 *   ~/.openclaw/workspace/CompanyA-local/digests/LITTLEBIRD_NOTES.md   (meeting summaries + tldr)
 *   ~/.openclaw/workspace/CompanyA-local/digests/LITTLEBIRD_NOTES.json (queryable: meetings + journals)
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const fs = require('fs'), path = require('path');
const CDP = process.env.LB_WEB_CDP || 'http://127.0.0.1:18821';
// Output dir. Defaults to Operator's CompanyA work digests (unchanged). Override with LB_DIG
// to run an isolated second tenant (e.g. Operator2) that must NOT write into the work lane.
const DIG = process.env.LB_DIG || path.join(process.env.HOME, '.openclaw/workspace/CompanyA-local/digests');
const JSON_ONLY = process.argv.includes('--json');
const MAX = Number(process.env.LB_MAX || 250);

(async () => {
  let b;
  try { b = await chromium.connectOverCDP(CDP); }
  catch (e) { console.error(`[lb-web] cannot connect ${CDP} — relaunch the profile. ${e.message}`); process.exit(2); }
  const ctx = b.contexts()[0];
  let pg = ctx.pages().find(p => /littlebird\.ai/.test(p.url())) || ctx.pages()[0] || await ctx.newPage();
  if (!/littlebird\.ai/.test(pg.url())) { try { await pg.goto('https://app.littlebird.ai/chats', { waitUntil: 'domcontentloaded', timeout: 30000 }); } catch {} }
  await pg.waitForTimeout(2500);

  const data = await pg.evaluate(async (max) => {
    const open = n => new Promise(r => { const q = indexedDB.open(n); q.onsuccess = () => r(q.result); q.onerror = () => r(null); setTimeout(() => r(null), 6000); });
    const readAll = (db, store) => new Promise(r => { try { const rows = []; const cr = db.transaction(store, 'readonly').objectStore(store).openCursor(); cr.onsuccess = e => { const c = e.target.result; if (c) { rows.push(c.value); c.continue(); } else r(rows); }; cr.onerror = () => r(rows); setTimeout(() => r(rows), 9000); } catch (e) { r([]); } });
    const db = await open('littlebird');
    if (!db) return { error: 'cannot open littlebird IDB — not logged in?' };
    const has = s => db.objectStoreNames.contains(s);
    const meetings = has('meetings') ? await readAll(db, 'meetings') : [];
    const journals = has('journals') ? await readAll(db, 'journals') : [];
    db.close();
    const txt = v => typeof v === 'string' ? v : (v == null ? '' : JSON.stringify(v));
    // PRIVACY FIREWALL (HARD): LittleBird captures ALL calls, work + personal. Only WORK calls
    // may land in the CompanyA work digest. Judge by TITLE + TLDR ONLY (pii-allow: employer token is intentional, internal work-lane tooling)
    // contain small talk (kids/weekend/wedding) that would false-drop the whole meeting.
    // Order is privacy-first: a personal signal drops even if a work keyword is also present.
    const PERSONAL_RE = /love you|family call|call home|airport call|\bthe goat\b|camp logistics|wedding|getting dressed|\bmeds\b|family check|facetime/i;
    const PERSONAL_APP = /avconferenced|facetime/i;
    const WORK_RE = /salesforce|\bdpp\b|dealer|lightning|sentinelone|password|CompanyA|sprint|\bapex\b|deploy|rebate|\bqad\b|tavant|standup|stand-up|databricks|monday|ticket|\bsf-?\d|okta|\bvpn\b|it support|tech support|onedrive|outlook|permission set|flow\b|quote|warranty|migration|uat|\bsit\b|report|portal|integration|sandbox|co-?op|einstein|dashboard|record|case|status|demo|planning|project|development|data/i; // pii-allow: 'CompanyA' work-topic keyword is intentional
    const isWork = m => {
      const title = (m.name || '').toLowerCase();
      const tldr = (txt(m.tldr) || '').toLowerCase();
      const att = (Array.isArray(m.attendees) ? m.attendees : []).map(a => (a && (a.email || a.name)) || a).join(' ').toLowerCase();
      if (PERSONAL_RE.test(title)) return false;         // personal marker in the TITLE -> decisive drop (a work call is never titled "Camp Logistics")
      if (att.includes('CompanyA')) return true;          // colleague present -> work, keep (pii-allow: intentional)
      if (WORK_RE.test(`${title} ${tldr}`)) return true; // explicit work topic -> keep (beats incidental small-talk in the tldr)
      if (PERSONAL_RE.test(tldr)) return false;          // personal marker only in tldr, no work signal -> drop
      if (PERSONAL_APP.test(m.sourceApp || '')) return false; // facetime/phone personal channel, no work signal -> drop
      return true;                                       // neutral -> keep (don't lose work calls with plain titles)
    };
    const M = meetings.filter(isWork).map(m => ({
      id: m.id, name: m.name || '(untitled)', createdAt: m.createdAt, sortAt: m.sortAt || m.lastTranscribedAt || m.createdAt,
      tldr: txt(m.tldr).slice(0, 600), summary: txt(m.summary).slice(0, 8000),
      transcript: txt(m.personifiedTranscript).slice(0, 40000),
      sourceApp: m.sourceApp || '', status: m.status || '',
      attendees: Array.isArray(m.attendees) ? m.attendees.map(a => (a && (a.name || a.email)) || a).filter(Boolean) : [],
    })).sort((a, b) => new Date(b.sortAt) - new Date(a.sortAt)).slice(0, max);
    const J = journals.map(j => ({ id: j.id, date: j.date, text: txt(j.text).slice(0, 8000) }))
      .sort((a, b) => new Date(b.date) - new Date(a.date));
    return { meetings: M, journals: J };
  }, MAX);

  if (data.error) { console.error('[lb-web] ' + data.error); process.exit(3); }
  if (JSON_ONLY) { console.log(JSON.stringify(data)); await b.close(); return; }

  fs.mkdirSync(DIG, { recursive: true });
  const ts = new Date().toISOString();
  fs.writeFileSync(path.join(DIG, 'LITTLEBIRD_NOTES.json'), JSON.stringify({ generated: ts, ...data }, null, 2));
  const fmt = s => { try { return new Date(s).toISOString().replace('T', ' ').slice(0, 16); } catch { return String(s || ''); } };
  const lines = [`# Littlebird — meeting notes (web bridge)`, `_generated ${ts} · ${data.meetings.length} meetings · ${data.journals.length} daily summaries_`, ''];
  for (const m of data.meetings.slice(0, 80)) {
    lines.push(`### ${m.name} — ${fmt(m.sortAt)}${m.sourceApp ? ' · ' + m.sourceApp : ''}`);
    if (m.attendees.length) lines.push(`- attendees: ${m.attendees.join(', ')}`);
    if (m.tldr) lines.push(`- TLDR: ${m.tldr}`);
    lines.push('');
  }
  fs.writeFileSync(path.join(DIG, 'LITTLEBIRD_NOTES.md'), lines.join('\n'));
  console.log(`[lb-web] ok: ${data.meetings.length} meetings, ${data.journals.length} journals -> LITTLEBIRD_NOTES.{md,json}`);
  await b.close();
})();
