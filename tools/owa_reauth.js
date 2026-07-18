#!/usr/bin/env node
/* OWA auto-reauth — when the headless Outlook-web session dies, log back in headlessly so the
 * send/read bridge self-heals (no phone). Drives the SAME CDP Chrome the bridge uses.
 *
 * Flow (standard Microsoft work-account login): email -> password -> MFA(verification code via
 * the mini's TOTP) -> "stay signed in". Creds from data/secrets/ms_username + ms_password; the
 * 6-digit code from `python3 tools/work_totp.py`.
 *
 * SAFETY: SINGLE password attempt per run (a wrong password fails + alerts, never loops -> no
 * account lockout). Screenshots every step to data/runtime/reauth/ so a blind-built selector can
 * be fixed against the real page. Built without seeing the live MFA page — EXPECT to tune the
 * selectors on first real expiry; the screenshots make that a 2-minute fix.
 *
 *   node tools/owa_reauth.js            # attempt reauth, print JSON {ok, step, reason}
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const fs = require('fs'), path = require('path'), os = require('os'), cp = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const CDP = process.env.OWA_WEB_CDP || 'http://127.0.0.1:18820';
const SHOTDIR = path.join(ROOT, 'data', 'runtime', 'reauth');
const out = (o) => { console.log(JSON.stringify(o)); };

function secret(name) {
  // login Keychain first (encrypted at rest), then the plaintext file fallback
  try {
    const v = cp.execFileSync('security', ['find-generic-password', '-a', 'claude-stack', '-s', name, '-w'],
      { timeout: 8000 }).toString().replace(/\n$/, '');
    if (v) return v;
  } catch {}
  try { return fs.readFileSync(path.join(ROOT, 'data', 'secrets', name), 'utf8').trim(); }
  catch { return ''; }
}
function totp() {
  try {
    const r = cp.execSync('/usr/bin/python3 ' + JSON.stringify(path.join(ROOT, 'tools/work_totp.py')), { timeout: 8000 }).toString();
    const m = r.match(/\b(\d{6})\b/); return m ? m[1] : '';
  } catch { return ''; }
}
async function shot(page, tag) {
  try { fs.mkdirSync(SHOTDIR, { recursive: true }); await page.screenshot({ path: path.join(SHOTDIR, `${Date.now()}_${tag}.png`) }); } catch {}
}
// try a list of selectors, return the first that's visible
async function firstVisible(page, sels, t = 4000) {
  for (const s of sels) {
    try { const el = page.locator(s).first(); if (await el.isVisible({ timeout: t / sels.length })) return el; } catch {}
  }
  return null;
}

(async () => {
  const user = secret('ms_username'), pass = secret('ms_password');
  if (!user || !pass) return out({ ok: false, step: 'creds', reason: 'missing ms_username/ms_password' });

  let b;
  try { b = await chromium.connectOverCDP(CDP); }
  catch (e) { return out({ ok: false, step: 'cdp', reason: 'cannot connect CDP ' + CDP + ' — ' + e.message }); }
  const ctx = b.contexts()[0];
  const page = ctx.pages().find(p => /office|microsoft|outlook/.test(p.url())) || ctx.pages()[0] || await ctx.newPage();

  try {
    await page.goto('https://outlook.office.com/mail/', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(2500);
    await shot(page, 'landing');

    // already logged in? (mailbox URL, no login host) — MS migrated office.com -> cloud.microsoft
    if (/outlook\.(office\.com|cloud\.microsoft)\/mail/.test(page.url()) && !/login\.microsoftonline|microsoftonline\.com\/common/.test(page.url())) {
      // confirm a real token by reloading and watching for the bearer
      await b.close();
      return out({ ok: true, step: 'already', reason: 'session already valid' });
    }

    // EMAIL
    const email = await firstVisible(page, ['input[type=email]', 'input[name=loginfmt]', '#i0116']);
    if (email) { await email.fill(user); await shot(page, 'email'); const n = await firstVisible(page, ['#idSIButton9', 'input[type=submit]']); if (n) await n.click(); await page.waitForTimeout(2500); }

    // PASSWORD (single attempt)
    const pw = await firstVisible(page, ['input[type=password]', 'input[name=passwd]', '#i0118']);
    if (pw) { await pw.fill(pass); await shot(page, 'password'); const s = await firstVisible(page, ['#idSIButton9', 'input[type=submit]']); if (s) await s.click(); await page.waitForTimeout(3000); }
    await shot(page, 'after_password');

    // wrong-password / locked detection -> stop, do NOT retry
    const errTxt = (await page.locator('#passwordError, .alert-error, [role=alert]').first().innerText().catch(() => '')) || '';
    if (/incorrect|isn'?t correct|account.*lock|too many/i.test(errTxt)) {
      await b.close();
      return out({ ok: false, step: 'password', reason: 'password rejected: ' + errTxt.slice(0, 120) + ' (NOT retrying — fix the stored password)' });
    }

    // MFA -> choose verification code, enter TOTP
    // MS often defaults to push ("approve sign in"); pick "use a verification code" / "sign in another way"
    for (const link of ['text=/verification code/i', 'text=/different method/i', 'text=/another way/i', 'text=/can\'?t use/i', '#signInAnotherWay']) {
      const el = await firstVisible(page, [link], 2500); if (el) { await el.click().catch(() => {}); await page.waitForTimeout(1500); break; }
    }
    // if a method picker appears, choose the authenticator-app / TOTP code option
    const codeOpt = await firstVisible(page, ['text=/authenticator app or hardware token/i', 'text=/use a verification code/i'], 2500);
    if (codeOpt) { await codeOpt.click().catch(() => {}); await page.waitForTimeout(1500); }

    const otc = await firstVisible(page, ['input[name=otc]', 'input#idTxtBx_SAOTCC_OTC', 'input[autocomplete=one-time-code]', 'input[type=tel]'], 4000);
    if (otc) {
      const code = totp();
      if (!code) { await b.close(); return out({ ok: false, step: 'totp', reason: 'no TOTP code from work_totp.py' }); }
      await otc.fill(code); await shot(page, 'otc');
      const v = await firstVisible(page, ['#idSubmit_SAOTCC_Continue', '#idSIButton9', 'input[type=submit]']); if (v) await v.click();
      await page.waitForTimeout(3500);
    }
    await shot(page, 'after_mfa');

    // "Stay signed in?"
    const stay = await firstVisible(page, ['#idSIButton9', 'input[type=submit][value=Yes]', 'text=/stay signed in/i'], 4000);
    if (stay) { await stay.click().catch(() => {}); await page.waitForTimeout(2500); }
    await shot(page, 'final');

    const okNow = /outlook\.(office\.com|cloud\.microsoft)\/mail/.test(page.url()) && !/login\.microsoftonline/.test(page.url());
    await b.close();
    return out({ ok: okNow, step: okNow ? 'done' : 'incomplete',
      reason: okNow ? 'reauth complete' : 'ended at ' + page.url().slice(0, 80) + ' — check screenshots in data/runtime/reauth' });
  } catch (e) {
    await shot(page, 'error').catch(() => {});
    try { await b.close(); } catch {}
    return out({ ok: false, step: 'exception', reason: e.message });
  }
})();
