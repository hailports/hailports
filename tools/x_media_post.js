#!/usr/bin/env node
/* X media poster — the piece the content engine never had: actually ATTACHES an image
 * or video and posts it, against a live logged-in CDP session, then VERIFIES the post is
 * live by finding it on the profile timeline (matched by freshness + media, skipping the
 * pinned tweet). Returns the real status URL on stdout as JSON, or a non-zero exit.
 *
 *   node tools/x_media_post.js --port 18800 --handle Ima_Furad --media /path.png --text "caption"
 *   node tools/x_media_post.js --port 18800 --handle Ima_Furad --media /clip.mp4 --text "caption"
 *
 * Headless-safe: connects to an already-running CDP profile, never foregrounds anything.
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const arg = k => { const i = process.argv.indexOf('--' + k); return i >= 0 ? process.argv[i + 1] : ''; };
const PORT = arg('port') || '18800';
const HANDLE = (arg('handle') || '').replace(/^@/, '');
const MEDIA = arg('media');
const TEXT = arg('text') || '';
const IS_VIDEO = /\.(mp4|mov|m4v)$/i.test(MEDIA || '');

(async () => {
  if (!HANDLE || !MEDIA) { console.error('need --handle and --media'); process.exit(2); }
  let b;
  try { b = await chromium.connectOverCDP('http://127.0.0.1:' + PORT); }
  catch (e) { console.error(JSON.stringify({ ok: false, error: 'cannot connect CDP ' + PORT + ': ' + e.message })); process.exit(3); }
  const ctx = b.contexts()[0];
  const pg = ctx.pages().find(p => /x\.com/.test(p.url())) || await ctx.newPage();

  await pg.goto('https://x.com/compose/post', { waitUntil: 'domcontentloaded', timeout: 25000 });
  await pg.waitForTimeout(4000);

  // confirm session is live (composer present) before doing anything
  const composer = await pg.$('[data-testid="tweetTextarea_0"], [role="textbox"]');
  if (!composer) { console.error(JSON.stringify({ ok: false, error: 'NOT logged in / no composer — re-login ' + HANDLE })); await b.close(); process.exit(4); }

  // attach media via the hidden file input
  try {
    const fi = await pg.waitForSelector('input[type=file][data-testid="fileInput"], input[data-testid="fileInput"], input[type=file]', { timeout: 8000 });
    await fi.setInputFiles(MEDIA);
  } catch (e) { console.error(JSON.stringify({ ok: false, error: 'file input not found: ' + e.message.slice(0, 80) })); await b.close(); process.exit(5); }
  // video needs longer to upload/process than an image
  await pg.waitForTimeout(IS_VIDEO ? 25000 : 9000);

  if (TEXT) { const ed = await pg.$('[data-testid="tweetTextarea_0"], [role="textbox"]'); await ed.click(); await pg.keyboard.type(TEXT, { delay: 16 }); await pg.waitForTimeout(2500); }

  let clicked = false;
  for (const sel of ['[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]']) {
    const btn = await pg.$(sel);
    if (btn && await btn.isEnabled().catch(() => false)) { await btn.click(); clicked = true; break; }
  }
  if (!clicked) { console.error(JSON.stringify({ ok: false, error: 'post button never enabled (media still uploading?)' })); await b.close(); process.exit(6); }
  await pg.waitForTimeout(IS_VIDEO ? 12000 : 8000);

  // VERIFY: find the fresh tweet on the timeline (skip pinned), match recency + media
  await pg.goto('https://x.com/' + HANDLE, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await pg.waitForTimeout(5000);
  const found = await pg.evaluate(() => {
    for (const a of [...document.querySelectorAll('article')].slice(0, 6)) {
      if (/Pinned/.test(a.innerText || '')) continue;
      const t = a.querySelector('time'); if (!t) continue;
      const ageMs = Date.now() - new Date(t.getAttribute('datetime')).getTime();
      const link = [...a.querySelectorAll('a[href*="/status/"]')].map(x => x.getAttribute('href')).find(h => /status\/\d+/.test(h));
      const hasMedia = !!a.querySelector('[data-testid="tweetPhoto"] img, [data-testid="videoPlayer"], video');
      if (ageMs < 240000 && link) return { link, ageSec: Math.round(ageMs / 1000), hasMedia };
    }
    return null;
  });
  await b.close();
  if (!found) { console.error(JSON.stringify({ ok: false, error: 'posted but could not verify a fresh tweet on timeline' })); process.exit(7); }
  console.log(JSON.stringify({ ok: true, url: 'https://x.com' + found.link, ageSec: found.ageSec, hasMedia: found.hasMedia }));
})();
