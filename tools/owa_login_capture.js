#!/usr/bin/env node
/* OWA login/MFA page CAPTURE — read-only harness to harvest the REAL Microsoft login + MFA
 * pages so owa_reauth.js's blind-built selectors can be tuned against ground truth.
 *
 * SAFE BY DESIGN — only meant to fire when the session is ALREADY dead (guardian confirms first):
 *  - opens a FRESH page in the bridge's CDP Chrome; never hijacks an existing bridge tab.
 *  - walks email -> password (SINGLE attempt, correct stored pw -> no lockout) to REACH the MFA page.
 *  - screenshots + dumps outerHTML at every step to data/runtime/reauth/capture_<ts>/.
 *  - STOPS at the MFA / verification-code page — never types the TOTP, never completes the login,
 *    so the "human does the final re-login" posture is left fully intact.
 *  - closes ONLY its own page (leaves the browser + every other tab untouched).
 *  - reports which selectors matched vs missed, so tuning owa_reauth.js is a diff, not a guess.
 *
 *   node tools/owa_login_capture.js       # print JSON {ok, dir, finalUrl, steps:[{step,url,sel,found}]}
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const fs = require('fs'), path = require('path'), cp = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const CDP = process.env.OWA_WEB_CDP || 'http://127.0.0.1:18820';
const DIR = path.join(ROOT, 'data', 'runtime', 'reauth', 'capture_' + Date.now());
const out = (o) => { console.log(JSON.stringify(o)); };
const steps = [];

function secret(name) {
  try {
    const v = cp.execFileSync('security', ['find-generic-password', '-a', 'claude-stack', '-s', name, '-w'],
      { timeout: 8000 }).toString().replace(/\n$/, '');
    if (v) return v;
  } catch {}
  try { return fs.readFileSync(path.join(ROOT, 'data', 'secrets', name), 'utf8').trim(); }
  catch { return ''; }
}
async function grab(page, step, selTried) {
  try { fs.mkdirSync(DIR, { recursive: true }); } catch {}
  let html = '';
  try { html = await page.content(); } catch {}
  try { fs.writeFileSync(path.join(DIR, step + '.html'), html); } catch {}
  try { await page.screenshot({ path: path.join(DIR, step + '.png'), fullPage: true }); } catch {}
  steps.push({ step, url: (page.url() || '').slice(0, 120), sel: selTried || null });
}
// return {el, sel} for the first visible selector, recording which one hit
async function firstVisible(page, sels, t = 4000) {
  for (const s of sels) {
    try { const el = page.locator(s).first(); if (await el.isVisible({ timeout: t / sels.length })) return { el, sel: s }; } catch {}
  }
  return { el: null, sel: null };
}

(async () => {
  const user = secret('ms_username'), pass = secret('ms_password');
  if (!user || !pass) return out({ ok: false, step: 'creds', reason: 'missing ms_username/ms_password' });

  let b, page;
  try { b = await chromium.connectOverCDP(CDP); }
  catch (e) { return out({ ok: false, step: 'cdp', reason: 'cannot connect CDP ' + CDP + ' — ' + e.message }); }
  const ctx = b.contexts()[0];
  if (!ctx) { try { await b.close(); } catch {} return out({ ok: false, step: 'ctx', reason: 'no CDP context' }); }

  try {
    page = await ctx.newPage();   // FRESH tab — never touch an existing bridge page
    await page.goto('https://outlook.office.com/mail/', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(2500);

    // session actually alive? bail BEFORE writing anything (no inbox screenshots on a warm run).
    // MS migrated the mailbox host office.com -> cloud.microsoft; match both, exclude login hosts.
    if (/outlook\.(office\.com|cloud\.microsoft)\/mail/.test(page.url()) && !/login\.microsoftonline|microsoftonline\.com\/common/.test(page.url())) {
      await page.close().catch(() => {}); try { await b.close(); } catch {}
      return out({ ok: true, step: 'already', reason: 'session still valid — nothing to capture', dir: null });
    }
    await grab(page, '01_landing');   // we're on a login page — safe to capture from here on

    // EMAIL
    const em = await firstVisible(page, ['input[type=email]', 'input[name=loginfmt]', '#i0116']);
    if (em.el) {
      await em.el.fill(user);
      const n = await firstVisible(page, ['#idSIButton9', 'input[type=submit]']);
      if (n.el) await n.el.click().catch(() => {});
      await page.waitForTimeout(2500);
    }
    await grab(page, '02_after_email', em.sel);

    // PASSWORD (single attempt; correct stored pw -> no lockout)
    const pw = await firstVisible(page, ['input[type=password]', 'input[name=passwd]', '#i0118']);
    if (pw.el) {
      await pw.el.fill(pass);
      const s = await firstVisible(page, ['#idSIButton9', 'input[type=submit]']);
      if (s.el) await s.el.click().catch(() => {});
      await page.waitForTimeout(3000);
    }
    await grab(page, '03_after_password', pw.sel);

    // wrong-password / locked -> STOP, never retry
    const errTxt = (await page.locator('#passwordError, .alert-error, [role=alert]').first().innerText().catch(() => '')) || '';
    if (/incorrect|isn'?t correct|account.*lock|too many/i.test(errTxt)) {
      await page.close().catch(() => {}); try { await b.close(); } catch {}
      return out({ ok: false, step: 'password', reason: 'password rejected: ' + errTxt.slice(0, 120) + ' — NOT retrying', dir: DIR, steps });
    }

    // MFA — nudge past a default push toward the verification-code entry, capturing each surface
    const switchLink = await firstVisible(page,
      ['text=/verification code/i', 'text=/different method/i', 'text=/another way/i', "text=/can'?t use/i", '#signInAnotherWay'], 2500);
    if (switchLink.el) { await switchLink.el.click().catch(() => {}); await page.waitForTimeout(1500); }
    await grab(page, '04_mfa_method', switchLink.sel);

    const codeOpt = await firstVisible(page, ['text=/authenticator app or hardware token/i', 'text=/use a verification code/i'], 2500);
    if (codeOpt.el) { await codeOpt.el.click().catch(() => {}); await page.waitForTimeout(1500); }

    // the OTC input — CAPTURE its selector match, but DO NOT fill (login stays incomplete on purpose)
    const otc = await firstVisible(page, ['input[name=otc]', 'input#idTxtBx_SAOTCC_OTC', 'input[autocomplete=one-time-code]', 'input[type=tel]'], 4000);
    await grab(page, '05_mfa_otc', otc.sel);

    const finalUrl = (page.url() || '').slice(0, 120);
    await page.close().catch(() => {});   // close ONLY our tab; leave Chrome + bridge intact
    try { await b.close(); } catch {}
    return out({
      ok: true, step: 'captured', dir: DIR, finalUrl,
      otcFound: !!otc.el, otcSel: otc.sel,
      reason: otc.el ? 'reached + captured the MFA verification-code page' : 'stopped short of OTC input — check screenshots',
      steps,
    });
  } catch (e) {
    if (page) await grab(page, '99_error').catch(() => {});
    try { if (page) await page.close(); } catch {}
    try { await b.close(); } catch {}
    return out({ ok: false, step: 'exception', reason: e.message, dir: DIR, steps });
  }
})();
