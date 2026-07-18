#!/usr/bin/env node
/* Outlook WEB work-context bridge — reads the mailbox by reusing OWA's OWN Bearer token
 * (audience outlook.office.com, minted by the normal OWA login) against the DOCUMENTED
 * Outlook REST API (/api/v2.0/me/messages). No Graph app, no consent, no token signing,
 * NO IT — attributed to the first-party Outlook Web App. Same technique as scripts/owa_fetch.sh
 * (which does attachment CONTENTS); this writes the inbox digest the work-GPT reads.
 *
 * Session: a PERSISTENT, already logged-in Chrome on ~/.chrome-cdp-profile-owa. Headless is
 * blocked by CompanyA Conditional Access, so the browser is real/visible (off-foreground) and
 * never killed; we only CONNECT over CDP (env OWA_WEB_CDP, default 127.0.0.1:18820) and read.
 *
 *   node tools/owa_web_bridge.js
 * Output: ~/.openclaw/workspace/CompanyA-local/digests/OUTLOOK_INBOX.{md,json}
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const https = require('https'), fs = require('fs'), path = require('path');
const CDP = process.env.OWA_WEB_CDP || 'http://127.0.0.1:18820';
const DIG = path.join(process.env.HOME, '.openclaw/workspace/CompanyA-local/digests');
const MODE = (process.argv.includes('--unread') || process.env.OWA_UNREAD_ONLY) ? 'unread' : 'recent';
const MAX = Number(process.env.OWA_MAX || (MODE === 'unread' ? 600 : 120));  // hard cap on rows pulled

const apiGet = (token, p) => new Promise(res => {
  https.get('https://outlook.office.com' + p, { headers: { Authorization: 'Bearer ' + token, Accept: 'application/json' } }, r => {
    let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res({ s: r.statusCode, j: JSON.parse(d) }); } catch { res({ s: r.statusCode, raw: d.slice(0, 200) }); } });
  }).on('error', e => res({ s: 0, err: e.message }));
});
// follow @odata.nextLink (full URL) to page beyond the per-request cap, up to `max` rows
const apiGetAll = async (token, firstPath, max) => {
  const rows = []; let p = firstPath;
  while (p && rows.length < max) {
    const r = await apiGet(token, p);
    if (r.s !== 200) return { s: r.s, raw: r.raw || r.err, rows };
    rows.push(...(r.j.value || []));
    const next = r.j['@odata.nextLink'];
    p = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
  }
  return { s: 200, rows: rows.slice(0, max) };
};
const fmtT = s => { try { return new Date(s).toISOString().replace('T', ' ').slice(0, 16); } catch { return String(s || ''); } };

(async () => {
  let b;
  try { b = await chromium.connectOverCDP(CDP); }
  catch (e) {
    try { require('child_process').execSync('bash ' + require('path').join(__dirname, '../scripts/owa_web_sync.sh'), { timeout: 60000, stdio: 'ignore' }); } catch (e2) {}
    await new Promise(r => setTimeout(r, 14000));
    try { b = await chromium.connectOverCDP(CDP); }
    catch (e3) { console.error(`[owa-web] cannot connect ${CDP} — relaunch the logged-in profile. ${e3.message}`); process.exit(2); }
  }
  const ctx = b.contexts()[0];
  const page = ctx.pages().find(p => /office|microsoft/.test(p.url())) || ctx.pages()[0];

  // capture the mail Bearer token the OWA SPA already uses
  let token = null;
  page.on('request', req => { const h = req.headers(); if (h.authorization && h.authorization.startsWith('Bearer ') && /outlook\.(cloud\.microsoft|office)/.test(req.url())) token = h.authorization.slice(7); });
  try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 40000 }); } catch {}
  for (let i = 0; i < 32 && !token; i++) await page.waitForTimeout(1000);
  await b.close();
  if (!token) { console.error('[owa-web] NOT logged in / no mail token captured — re-login needed'); process.exit(3); }

  // TRUE folder counts (the real unread totals across the mailbox, not just what we list)
  const folders = [];
  const mf = await apiGet(token, '/api/v2.0/me/mailfolders?$top=50&$select=DisplayName,UnreadItemCount,TotalItemCount');
  if (mf.s === 200) for (const f of (mf.j.value || [])) folders.push({ name: f.DisplayName, unread: f.UnreadItemCount || 0, total: f.TotalItemCount || 0 });
  const inboxUnreadTrue = (folders.find(f => /^inbox$/i.test(f.name)) || {}).unread || 0;

  // message list: unread-only (fully paged up to MAX) or recent. Inbox folder scope.
  const sel = 'Subject,From,ReceivedDateTime,IsRead,BodyPreview,HasAttachments,WebLink';
  const base = '/api/v2.0/me/mailfolders/inbox/messages';
  const q = MODE === 'unread'
    ? `${base}?$filter=IsRead eq false&$orderby=ReceivedDateTime desc&$top=100&$select=${sel}`
    : `${base}?$orderby=ReceivedDateTime desc&$top=100&$select=${sel}`;
  const r = await apiGetAll(token, q, MAX);
  if (r.s !== 200) { console.error('[owa-web] REST messages failed:', r.s, r.raw || ''); process.exit(4); }

  const rows = r.rows.map(m => ({
    subject: m.Subject || '(no subject)',
    from: (m.From && m.From.EmailAddress && (m.From.EmailAddress.Name || m.From.EmailAddress.Address)) || '',
    received: m.ReceivedDateTime || '',
    preview: (m.BodyPreview || '').replace(/\s+/g, ' ').slice(0, 220),
    unread: m.IsRead === false,
    hasAttachments: !!m.HasAttachments,
  }));

  fs.mkdirSync(DIG, { recursive: true });
  const ts = new Date().toISOString();
  const listedUnread = rows.filter(x => x.unread).length;
  const capped = rows.length >= MAX;
  fs.writeFileSync(path.join(DIG, 'OUTLOOK_INBOX.json'), JSON.stringify(
    { generated: ts, mode: MODE, listed: rows.length, listed_unread: listedUnread, inbox_unread_true: inboxUnreadTrue, capped, folders, rows }, null, 2));
  const lines = [
    `# Outlook — ${MODE === 'unread' ? 'UNREAD inbox' : 'inbox'} (web bridge, REST)`,
    `_generated ${ts} · listing ${rows.length} msgs${capped ? ` (capped at ${MAX})` : ''} · inbox unread TRUE total: ${inboxUnreadTrue}_`,
    '',
    '## Folder counts (true)',
    ...folders.map(f => `- **${f.name}**: ${f.unread} unread / ${f.total} total`),
    '',
    '## Messages',
  ];
  for (const m of rows) {
    lines.push(`### ${m.unread ? '🔵 ' : ''}${m.subject}${m.hasAttachments ? '  📎' : ''}`);
    lines.push(`- from: ${m.from || '?'} · ${fmtT(m.received)}`);
    if (m.preview) lines.push(`- ${m.preview}`);
    lines.push('');
  }
  fs.writeFileSync(path.join(DIG, 'OUTLOOK_INBOX.md'), lines.join('\n'));
  console.log(`[owa-web] ok: listed ${rows.length} (${listedUnread} unread shown) · inbox TRUE unread=${inboxUnreadTrue}${capped ? ` · CAPPED@${MAX}` : ''} -> OUTLOOK_INBOX.{md,json}`);

  // recent SENT — so the stack/GPT pick up "oh, that already went out" instead of re-drafting/asking
  try {
    const ssel = 'Subject,ToRecipients,CcRecipients,SentDateTime,BodyPreview';
    const sr = await apiGet(token, `/api/v2.0/me/mailfolders/sentitems/messages?$orderby=SentDateTime desc&$top=40&$select=${ssel}`);
    if (sr.s === 200) {
      const nm = t => (t && t.EmailAddress && (t.EmailAddress.Name || t.EmailAddress.Address)) || '';
      const sent = (sr.j.value || []).map(m => ({
        subject: m.Subject || '(no subject)',
        to: (m.ToRecipients || []).map(nm).filter(Boolean).join(', '),
        cc: (m.CcRecipients || []).map(nm).filter(Boolean).join(', '),
        sent: m.SentDateTime || '',
        preview: (m.BodyPreview || '').replace(/\s+/g, ' ').slice(0, 200),
      }));
      fs.writeFileSync(path.join(DIG, 'OUTLOOK_SENT.json'), JSON.stringify({ generated: ts, count: sent.length, sent }, null, 2));
      const sl = [`# Outlook — recent SENT (web bridge, REST)`, `_generated ${ts} · ${sent.length} recent sends — check before drafting/asking_`, ''];
      for (const m of sent) {
        sl.push(`### ${m.subject}`);
        sl.push(`- to: ${m.to}${m.cc ? ' · cc: ' + m.cc : ''} · sent ${fmtT(m.sent)}`);
        if (m.preview) sl.push(`- ${m.preview}`);
        sl.push('');
      }
      fs.writeFileSync(path.join(DIG, 'OUTLOOK_SENT.md'), sl.join('\n'));
      console.log(`[owa-web] sent: ${sent.length} recent -> OUTLOOK_SENT.{md,json}`);
    }
  } catch (e) { console.error('[owa-web] sent pull failed', e.message); }
})();
