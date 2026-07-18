#!/usr/bin/env node
/* Outlook READ bridge — mail + calendar straight from Outlook on the Web via the documented
 * REST API (/api/v2.0), reusing OWA's own first-party token from the logged-in CDP session.
 * No native app, no IT. Companion to owa_write.js (drafts/send). Replaces the native SQLite
 * reads (outlook_local/outlook_calendar_sqlite) so the Mac Outlook app can be uninstalled.
 *
 *   node tools/owa.js inbox [--top 25] [--unread]
 *   node tools/owa.js unread                       # true unread counts per folder
 *   node tools/owa.js agenda                        # today's events
 *   node tools/owa.js upcoming [--days 7]
 *   node tools/owa.js search-events --q "UAT" [--days 30]
 *   node tools/owa.js read --match "subject text"   # newest matching message, full body
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const https = require('https');
const CDP = process.env.OWA_WEB_CDP || 'http://127.0.0.1:18820';
const A = process.argv.slice(2);
const cmd = A[0];
const arg = (k, d = '') => { const i = A.indexOf('--' + k); return i >= 0 ? A[i + 1] : d; };
const flag = k => A.includes('--' + k);

const get = (token, p) => new Promise(res => {
  https.get('https://outlook.office.com' + p, { headers: { Authorization: 'Bearer ' + token, Accept: 'application/json' } },
    r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res({ s: r.statusCode, j: JSON.parse(d) }); } catch { res({ s: r.statusCode, raw: d.slice(0, 200) }); } }); }).on('error', e => res({ s: 0, err: e.message }));
});
const _ctParts = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const fmt = s => { try { let v = s; if (typeof v === 'string') { const tv = v.trim(); if (/\d{2}:\d{2}/.test(tv) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(tv)) v = tv.replace(/(\.\d{3})\d+$/, '$1') + 'Z'; } const d = new Date(v); if (isNaN(d)) return String(s || ''); const p = {}; for (const x of _ctParts.formatToParts(d)) p[x.type] = x.value; return `${p.year}-${p.month}-${p.day} ${p.hour === '24' ? '00' : p.hour}:${p.minute}`; } catch { return String(s || ''); } };
const strip = h => (h || '').replace(/<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/gi, ' ').replace(/&amp;/gi, '&').replace(/\s+/g, ' ').trim();

const _fs = require('fs');
const _TOKCACHE = '/home/user/claude-stack/data/runtime/owa_token.json';
const _TOK_TTL_MS = 40 * 60 * 1000;   // OWA tokens live ~1hr; treat as good for 40min so reads never hit expiry
function _cachedToken() { try { const c = JSON.parse(_fs.readFileSync(_TOKCACHE, 'utf8')); if (c && c.token && (Date.now() - c.ts) < _TOK_TTL_MS) return c.token; } catch (e) {} return null; }

(async () => {
  // FAST PATH: a fresh cached token = ZERO browser (a plain HTTPS API call). Only cold-capture
  // (attach the OWA Chrome + sniff the token) when the cache is missing/stale, and refresh the
  // cache when we do — so the OWA web Chrome can stay OFF except during a ~20s token refresh.
  let token = _cachedToken();
  if (!token) {
    let b;
    try { b = await chromium.connectOverCDP(CDP); }
    catch (e) {
      // OWA is on-demand (down) — spawn it once, then retry the attach so a cold read never
      // just fails. Rare path (only when the token cache is stale); the refresher keeps it warm.
      try { require('child_process').execSync('bash ' + require('path').join(__dirname, '../scripts/owa_web_sync.sh'), { timeout: 60000, stdio: 'ignore' }); } catch (e2) {}
      await new Promise(r => setTimeout(r, 14000));
      try { b = await chromium.connectOverCDP(CDP); }
      catch (e3) { console.log('NOT logged in / OWA profile down: ' + e3.message); process.exit(2); }
    }
    const page = b.contexts()[0].pages().find(p => /office|microsoft/.test(p.url())) || b.contexts()[0].pages()[0];
    page.on('request', q => { const h = q.headers(); if (h.authorization && h.authorization.startsWith('Bearer ') && /outlook\.(cloud\.microsoft|office)/.test(q.url())) token = h.authorization.slice(7); });
    try { await page.reload({ waitUntil: 'domcontentloaded' }); } catch {}
    for (let i = 0; i < 32 && !token; i++) await page.waitForTimeout(1000);
    await b.close();
    if (!token) { console.log('NOT logged in — re-login the OWA profile'); process.exit(3); }
    try { _fs.writeFileSync(_TOKCACHE, JSON.stringify({ token, ts: Date.now() })); } catch (e) {}
  }

  const isoDay = off => { const d = new Date(); d.setUTCDate(d.getUTCDate() + off); return d.toISOString(); };

  if (cmd === 'unread') {
    const r = await get(token, '/api/v2.0/me/mailfolders?$top=50&$select=DisplayName,UnreadItemCount,TotalItemCount');
    if (r.s !== 200) return console.log('error ' + r.s);
    for (const f of (r.j.value || [])) console.log(`${f.DisplayName}: ${f.UnreadItemCount} unread / ${f.TotalItemCount}`);
    return;
  }
  if (cmd === 'inbox') {
    const top = Number(arg('top', '25'));
    const sel = 'Subject,From,ReceivedDateTime,IsRead,BodyPreview,HasAttachments';
    const filt = flag('unread') ? '$filter=IsRead eq false&' : '';
    const r = await get(token, `/api/v2.0/me/mailfolders/inbox/messages?${filt}$top=${top}&$orderby=ReceivedDateTime desc&$select=${sel}`);
    if (r.s !== 200) return console.log('error ' + r.s + ' ' + (r.raw || ''));
    for (const m of (r.j.value || [])) console.log(`${m.IsRead ? '  ' : '🔵 '}${(m.Subject || '(no subject)')} — ${(m.From && m.From.EmailAddress && m.From.EmailAddress.Name) || ''} · ${fmt(m.ReceivedDateTime)}${m.HasAttachments ? ' 📎' : ''}\n   ${(m.BodyPreview || '').replace(/\s+/g, ' ').slice(0, 160)}`);
    return;
  }
  if (cmd === 'agenda' || cmd === 'upcoming') {
    const days = cmd === 'agenda' ? 1 : Number(arg('days', '7'));
    const r = await get(token, `/api/v2.0/me/calendarview?startDateTime=${isoDay(0)}&endDateTime=${isoDay(days)}&$orderby=Start/DateTime&$top=50&$select=Subject,Start,End,Location,Organizer,IsAllDay`);
    if (r.s !== 200) return console.log('error ' + r.s + ' ' + (r.raw || ''));
    const ev = r.j.value || [];
    if (!ev.length) return console.log(cmd === 'agenda' ? 'No events today.' : `No events in the next ${days} days.`);
    for (const e of ev) console.log(`${fmt(e.Start && e.Start.DateTime)} — ${e.Subject || '(untitled)'}${e.Location && e.Location.DisplayName ? ' @ ' + e.Location.DisplayName : ''}${e.Organizer && e.Organizer.EmailAddress ? ' · ' + e.Organizer.EmailAddress.Name : ''}`);
    return;
  }
  if (cmd === 'search-events') {
    const q = (arg('q') || '').toLowerCase(); const days = Number(arg('days', '30'));
    const r = await get(token, `/api/v2.0/me/calendarview?startDateTime=${isoDay(-days)}&endDateTime=${isoDay(days)}&$orderby=Start/DateTime&$top=100&$select=Subject,Start,Location`);
    if (r.s !== 200) return console.log('error ' + r.s);
    const hits = (r.j.value || []).filter(e => (e.Subject || '').toLowerCase().includes(q));
    if (!hits.length) return console.log(`No events matching "${arg('q')}".`);
    for (const e of hits) console.log(`${fmt(e.Start && e.Start.DateTime)} — ${e.Subject}`);
    return;
  }
  if (cmd === 'read') {
    const m = arg('match');
    if (!m) return console.log('read needs --match');
    const f = await get(token, `/api/v2.0/me/messages?$search="${encodeURIComponent(m)}"&$top=1&$select=Subject,From,ReceivedDateTime,Body`);
    const hit = (f.j && f.j.value && f.j.value[0]) || null;
    if (!hit) return console.log(`No message matching "${m}".`);
    console.log(`Subject: ${hit.Subject}\nFrom: ${(hit.From && hit.From.EmailAddress && hit.From.EmailAddress.Address) || ''}\nReceived: ${fmt(hit.ReceivedDateTime)}\n\n${strip(hit.Body && hit.Body.Content).slice(0, 3000)}`);
    return;
  }
  if (cmd === 'index') {
    // JSON for the work-context indexer: recent inbox messages with REST id + meta (no body).
    // --unread filters to unread only (exec_assistant's unreplied scan).
    const top = Number(arg('top', '300'));
    const sel = 'Id,ConversationId,Subject,From,ToRecipients,CcRecipients,ReceivedDateTime,BodyPreview,IsRead';
    const uf = flag('unread') ? '$filter=IsRead%20eq%20false&' : '';
    const rows = []; let p = `/api/v2.0/me/mailfolders/inbox/messages?${uf}$top=100&$orderby=ReceivedDateTime desc&$select=${sel}`;
    while (p && rows.length < top) {
      const r = await get(token, p);
      if (r.s !== 200) { console.log(JSON.stringify({ error: r.s, raw: r.raw })); return; }
      for (const m of (r.j.value || [])) {
        const parts = [
          m.From && m.From.EmailAddress && (m.From.EmailAddress.Name || m.From.EmailAddress.Address),
          ...(m.ToRecipients || []).map(x => x.EmailAddress && x.EmailAddress.Address),
          ...(m.CcRecipients || []).map(x => x.EmailAddress && x.EmailAddress.Address),
        ].filter(Boolean).join(' · ');
        rows.push({ id: m.Id, subject: m.Subject || '(no subject)', parts, is_read: m.IsRead,
                    sender_name: (m.From && m.From.EmailAddress && m.From.EmailAddress.Name) || '',
                    sender_email: (m.From && m.From.EmailAddress && m.From.EmailAddress.Address) || '',
                    thread_id: m.ConversationId || '',
                    received: m.ReceivedDateTime, preview: (m.BodyPreview || '').replace(/\s+/g, ' ') });
      }
      const next = r.j['@odata.nextLink']; p = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
    }
    console.log(JSON.stringify(rows.slice(0, top)));
    return;
  }
  if (cmd === 'sent') {
    // JSON: recent SENT messages (subject + date) — for reply / awaiting-reply detection.
    const top = Number(arg('top', '200'));
    const sel = 'Id,Subject,ToRecipients,SentDateTime';
    const rows = []; let p = `/api/v2.0/me/mailfolders/sentitems/messages?$top=100&$orderby=SentDateTime desc&$select=${sel}`;
    while (p && rows.length < top) {
      const r = await get(token, p);
      if (r.s !== 200) { console.log(JSON.stringify({ error: r.s, raw: r.raw })); return; }
      for (const m of (r.j.value || [])) rows.push({ id: m.Id, subject: m.Subject || '(no subject)', sent: m.SentDateTime, to: (m.ToRecipients || []).map(x => x.EmailAddress && x.EmailAddress.Address).filter(Boolean).join(', ') });
      const next = r.j['@odata.nextLink']; p = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
    }
    console.log(JSON.stringify(rows.slice(0, top)));
    return;
  }
  if (cmd === 'body') {
    const id = arg('id'); if (!id) { console.log(''); return; }
    const r = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(id)}?$select=Body`);
    console.log(r.s === 200 ? strip(r.j.Body && r.j.Body.Content).slice(0, 8000) : '');
    return;
  }
  if (cmd === 'zoom-summaries') {
    // Zoom AI Companion summaries arrive as "Meeting assets ... are ready!" emails from
    // user@example.com — the body carries the actual summary. Return them as JSON for indexing.
    const top = Number(arg('top', '200'));
    const r = await get(token, `/api/v2.0/me/messages?$search="${encodeURIComponent('from:zoom.us meeting assets are ready')}"&$top=${top}&$select=Id,Subject,ReceivedDateTime,Body`);
    if (r.s !== 200) { console.log('[]'); return; }
    const out = [];
    for (const m of (r.j.value || [])) {
      if (!/meeting assets.*ready/i.test(m.Subject || '')) continue;
      let body = strip(m.Body && m.Body.Content);
      // drop the external-sender banner + de-dupe the doubled body Zoom emails ship
      body = body.replace(/ZjQcmQRYFpfpt[\s\S]*?ZjQcmQRYFpfptBannerEnd/g, ' ').replace(/\s+/g, ' ').trim();
      const half = body.slice(0, Math.floor(body.length / 2));
      if (body.slice(Math.floor(body.length / 2)).trim().startsWith(half.slice(0, 40).trim())) body = half;
      out.push({ id: m.Id, subject: m.Subject, received: m.ReceivedDateTime, body: body.slice(0, 8000) });
    }
    console.log(JSON.stringify(out));
    return;
  }
  if (cmd === 'with') {
    // Recent conversation with a person — BOTH directions (their emails to you + yours to them).
    // $search spans all folders (inbox + sent), so it surfaces the emails YOU sent them too.
    const who = (arg('who') || '').toLowerCase().trim();
    if (!who) { console.log(JSON.stringify({ error: 'with needs --who' })); return; }
    // Match on the SURNAME (last token). Outlook stores senders as "Last, First", so a full "first last"
    // substring never matches ("matt person" vs "Person, Matt"), and nicknames diverge from the legal
    // first name ("nick" vs "Nicholas"). The surname is the stable, order-independent anchor.
    const whoTokens = who.split(/[\s,]+/).filter(Boolean);
    const surname = whoTokens.length ? whoTokens[whoTokens.length - 1] : who;
    const days = Number(arg('days', '14'));
    const top = Number(arg('top', '30'));
    const cutoff = new Date(); cutoff.setUTCDate(cutoff.getUTCDate() - days);
    const sel = 'Subject,From,ToRecipients,CcRecipients,ReceivedDateTime,SentDateTime,Body';
    const r = await get(token, `/api/v2.0/me/messages?$search="${encodeURIComponent(surname)}"&$top=${Math.min(top * 2, 60)}&$select=${sel}`);
    if (!(r.s >= 200 && r.s < 300)) { console.log(JSON.stringify({ error: r.s, raw: r.raw || '' })); return; }
    const named = a => (((a && a.EmailAddress && ((a.EmailAddress.Name || '') + ' ' + (a.EmailAddress.Address || ''))) || '')).toLowerCase();
    const rows = [];
    for (const m of (r.j.value || [])) {
      const fromStr = named(m.From);
      const toArr = (m.ToRecipients || []).map(named);
      const ccArr = (m.CcRecipients || []).map(named);
      const fromMe = /Operator/.test(fromStr);                 // Operator
      const fromThem = fromStr.includes(surname);
      const toThem = toArr.some(t => t.includes(surname)) || ccArr.some(t => t.includes(surname));
      // DIRECT correspondence only: an email FROM the person, or one Operator SENT to the person. A third
      // party who merely CC's the person is EXCLUDED — that's what caused mis-attributed "they replied"
      // hallucinations (e.g. Cinthya CC'ing Rich showed up as a Rich reply).
      if (!(fromThem || (fromMe && toThem))) continue;
      const dt = m.ReceivedDateTime || m.SentDateTime || '';
      if (dt && new Date(dt) < cutoff) continue;
      const sender = (m.From && m.From.EmailAddress && (m.From.EmailAddress.Name || m.From.EmailAddress.Address)) || '';
      rows.push({
        direction: fromThem ? ('FROM ' + sender) : 'YOU SENT',  // real sender, not a guessed direction
        subject: m.Subject || '(no subject)',
        from: sender,
        to: (m.ToRecipients || []).map(x => x.EmailAddress && (x.EmailAddress.Name || x.EmailAddress.Address)).filter(Boolean).join(', '),
        date: dt,
        body: strip(m.Body && m.Body.Content).slice(0, 3000),
      });
    }
    rows.sort((a, b) => String(b.date).localeCompare(String(a.date)));
    console.log(JSON.stringify({ ok: true, who: arg('who'), days, count: rows.length, messages: rows.slice(0, top) }));
    return;
  }
  if (cmd === 'today') {
    // Recent emails BOTH directions (inbox received + sent), with bodies — default today; --days N
    // widens the window. For "did any answers come in" / morning-review / unblock scans. Direction by
    // real sender so replies aren't mis-attributed.
    const days = Math.max(1, Number(arg('days', '1')));
    const start = new Date(); start.setUTCHours(0, 0, 0, 0); start.setUTCDate(start.getUTCDate() - (days - 1));
    const sel = 'Subject,From,ToRecipients,ReceivedDateTime,SentDateTime,Body';
    const out = [];
    for (const folder of ['inbox', 'sentitems']) {
      const ord = folder === 'inbox' ? 'ReceivedDateTime' : 'SentDateTime';
      const r = await get(token, `/api/v2.0/me/mailfolders/${folder}/messages?$top=40&$orderby=${ord}%20desc&$select=${sel}`);
      for (const m of (r.j && r.j.value || [])) {
        const dt = m.ReceivedDateTime || m.SentDateTime || '';
        if (!dt || new Date(dt) < start) continue;
        const sender = (m.From && m.From.EmailAddress && (m.From.EmailAddress.Name || m.From.EmailAddress.Address)) || '';
        const addr = (m.From && m.From.EmailAddress && m.From.EmailAddress.Address) || '';
        const fromMe = /Operator|mocskonyi/i.test(sender + ' ' + addr);   // match his name OR address
        out.push({
          direction: fromMe ? 'YOU SENT' : ('FROM ' + sender),
          subject: m.Subject || '(no subject)',
          to: (m.ToRecipients || []).map(x => x.EmailAddress && (x.EmailAddress.Name || x.EmailAddress.Address)).filter(Boolean).join(', '),
          date: dt,
          body: strip(m.Body && m.Body.Content).slice(0, 2500),
        });
      }
    }
    out.sort((a, b) => String(b.date).localeCompare(String(a.date)));
    console.log(JSON.stringify({ ok: true, count: out.length, messages: out }));
    return;
  }
  console.log('unknown command: ' + cmd);
  process.exit(1);
})();
