#!/usr/bin/env python3
"""invariants_guard — holds Operator's locked decisions in place so they can't silently drift.

Each invariant has a CHECK and a real FIX. On drift:
  - config-value drift (e.g. work-GPT base URL, firewall) -> auto-corrected to the right value
  - dangerous drift (autonomous-write tool reappears, an ownership guard is removed) -> the drifted
    component is auto-NEUTRALIZED immediately (disabled) so the danger is gone, then alert
Every detection + fix is logged to SYSTEM_CHANGELOG.md and (on a state change) iMessaged to Operator.
Runs on a schedule (launchd) and on demand. $0, stdlib only.

  invariants_guard.py             # check all, auto-fix drift, alert on change
  invariants_guard.py --check     # report only, do NOT fix
"""
import json, os, re, subprocess, sys, time, urllib.request, ssl

ROOT = os.path.expanduser("~/claude-stack")
LA = os.path.expanduser("~/Library/LaunchAgents")
STATE = os.path.join(ROOT, "data", "focus", "invariants_state.json")
CHANGELOG = os.path.join(ROOT, "SYSTEM_CHANGELOG.md")
FW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
PLIST = os.path.join(LA, "com.claude-stack.chatgpt-CompanyA-action.plist")
SF_AGENT = os.path.join(ROOT, "agents", "sf_ticket_agent.py")
OD_SYNC = os.path.join(ROOT, "tools", "onedrive_work_sync.js")
AGENTS = os.path.expanduser("~/AGENTS.md")
DOING_FIX = "--check" not in sys.argv

def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)

def http_get(url, timeout=10):
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, str(e)

def reload_action_service():
    sh(["launchctl", "unload", PLIST]); time.sleep(1)
    sh(["launchctl", "load", PLIST])
    for _ in range(15):
        c, _o = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "3",
                    "http://127.0.0.1:8078/health"])
        if _o.strip() == "200":
            break
        time.sleep(1)

# ── invariant checks (return (ok, detail)) and fixes (return action string) ──────────────────

def chk_workgpt_base():
    # plist env + live advertised server must be work.Operator.com, never docsapp
    try:
        txt = open(PLIST).read()
    except Exception as e:
        return False, f"plist unreadable: {e}"
    m = re.search(r"CHATGPT_WORK_PUBLIC_BASE_URL</key>\s*<string>([^<]+)</string>", txt)
    env_val = (m.group(1) if m else "").strip()
    env_ok = env_val == "https://work.Operator.com"
    # live check against the local service (reliable; no DNS/SSL edge), via curl
    _c, body = sh(["curl", "-s", "--max-time", "8", "http://127.0.0.1:8078/openapi.json"])
    live_bad = "work.docsapp.dev" in body   # docsapp actually being served = real drift
    # Treat as drift ONLY on genuine signals (env wrong, or docsapp actually live). A transient
    # curl failure must NOT trigger an unnecessary service reload, so we don't require a positive
    # live hit when env is already correct.
    if not env_ok:
        return False, f"DRIFT plist env={env_val or '?'} (expected work.Operator.com)"
    if live_bad:
        return False, "DRIFT: live service still advertising work.docsapp.dev"
    return True, "base=work.Operator.com"

def fix_workgpt_base():
    txt = open(PLIST).read()
    if "work.docsapp.dev" in txt or "CHATGPT_WORK_PUBLIC_BASE_URL" in txt:
        bak = PLIST + f".bak-invguard-{int(time.time())}"
        open(bak, "w").write(txt)
        new = re.sub(r"(CHATGPT_WORK_PUBLIC_BASE_URL</key>\s*<string>)[^<]+(</string>)",
                     r"\1https://work.Operator.com\2", txt)
        open(PLIST, "w").write(new)
        reload_action_service()
        return "reset plist base URL -> work.Operator.com and reloaded service"
    return "could not locate base URL key to fix"

def chk_sf_no_autonomous_writes():
    try:
        src = open(SF_AGENT).read()
    except Exception as e:
        return False, f"sf_ticket_agent unreadable: {e}"
    m = re.search(r"_SAFE_AUTO_TOOLS\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        return False, "_SAFE_AUTO_TOOLS block not found"
    tools = re.findall(r'"([a-z_]+)"', m.group(1))
    writes = [t for t in tools if re.search(
        r"(transfer_record|move_contact|assign_permission|create_record_share|add_user_to_queue|"
        r"add_record_team|apply_access_fix|bulk_update|delete_record|merge_records|deploy_to_prod|"
        r"panic_revert|onboard_user|prepare_dealer|reassign)", t)]
    if not writes:
        return True, f"sf-agent auto-exec read-only ({len(tools)} tools)"
    return False, f"DANGER: write tools back in sf-agent auto-exec: {writes}"

def fix_sf_no_autonomous_writes():
    # real immediate fix: NEUTRALIZE — disable the agent so it cannot autonomously write
    label = "com.claude-stack.sf-agent"
    sh(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
    sh(["launchctl", "unload", os.path.join(LA, label + ".plist")])
    return f"NEUTRALIZED: disabled {label} (autonomous SF write tool reappeared) — needs manual repair"

def chk_onedrive_guard():
    try:
        src = open(OD_SYNC).read()
    except Exception as e:
        return False, f"onedrive_work_sync unreadable: {e}"
    if "OWN_ROOT" in src and "non-owned storage" in src and "process.exit(9)" in src:
        return True, "onedrive ownership guard present"
    return False, "DANGER: onedrive ownership guard missing"

def fix_onedrive_guard():
    label = "com.claude-stack.onedrive-work-sync"
    sh(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
    sh(["launchctl", "unload", os.path.join(LA, label + ".plist")])
    return f"NEUTRALIZED: disabled {label} (ownership guard missing) — needs manual repair"

def chk_firewall_remote_viewing():
    _c, st = sh([FW, "--getstealthmode"]); _c2, ba = sh([FW, "--getblockall"])
    stealth_on = "on" in st.lower() and "off" not in st.lower()
    blockall_on = "enabled" in ba.lower() and "disabled" not in ba.lower()
    if not stealth_on and not blockall_on:
        return True, "firewall: stealth off, block-all off (remote viewing reachable)"
    return False, f"DRIFT firewall stealth_on={stealth_on} blockall_on={blockall_on}"

def fix_firewall_remote_viewing():
    sh(["sudo", "-n", FW, "--setstealthmode", "off"])
    sh(["sudo", "-n", FW, "--setblockall", "off"])
    ok, _ = chk_firewall_remote_viewing()
    return "reverted firewall stealth/block-all OFF" if ok else "tried to revert firewall (needs sudoers grant for full auto-fix)"

def chk_agents_rules_present():
    try:
        a = open(AGENTS).read()
    except Exception as e:
        return False, f"AGENTS.md unreadable: {e}"
    need = ["WHO Operator IS", "READ-ONLY on anything we don't own"]
    missing = [n for n in need if n not in a]
    return (not missing), ("AGENTS.md rules present" if not missing else f"DRIFT missing in AGENTS.md: {missing}")

def fix_agents_rules_present():
    # prose can't be safely auto-regenerated; alert is the fix signal (high severity)
    return "ALERT-ONLY: AGENTS.md governing rules missing — restore from memory/backup manually"


def chk_corp_writes_gated():
    # Every Salesforce/Monday admin write tool MUST require explicit approval. A regression here =
    # the GPT could execute a prod write without Operator's confirmation. (The hole that existed 2026-06-13.)
    try:
        sys.path.insert(0, ROOT)
        import importlib
        import core.constants as c
        importlib.reload(c)
        gated = c.WRITE_TOOLS | c.APPROVAL_REQUIRED_TOOLS | c.DESTRUCTIVE_TOOLS
        # NOTE: sf_prepare_* and sf_onboard_user_from_template are intentionally NOT here — they are
        # dry-run PREVIEW generators (must stay direct so explicit_approval=false produces the preview);
        # their real write is gated by the apply_changes + explicit_approval guard in run_tool. Listing
        # them here would force them through the name-approval gate and break the preview-first flow.
        must = {
            "sf_assign_permission_set", "sf_remove_permission_set", "sf_apply_access_fix",
            "sf_create_field", "sf_update_picklist_deps", "sf_create_validation_rule",
            "sf_create_sharing_rule", "sf_merge_records", "sf_move_contact", "sf_transfer_record_owner",
            "sf_deploy_flow", "sf_restore_metadata_change", "sf_bulk_update", "sf_create_record",
            "sf_update_record", "monday_update_item", "monday_add_update", "monday_create_item",
        }
        ungated = sorted(must - gated)
        if ungated:
            return False, f"DANGER: ungated corp write tools (execute w/o approval): {ungated}"
        return True, f"all {len(must)} corp admin write tools require approval"
    except Exception as e:
        return False, f"gate check failed: {e}"


def fix_corp_writes_gated():
    return "ALERT-ONLY: a corp write tool lost its approval gate — re-add it to SALESFORCE_ADMIN_WRITE_TOOLS in core/constants.py immediately"

def chk_outlook_edit_draft_registered():
    files = {
        "tools/outlook_local.py": os.path.join(ROOT, "tools", "outlook_local.py"),
        "tools/owa_write.js": os.path.join(ROOT, "tools", "owa_write.js"),
        "apps/chatgpt_redacted_action.py": os.path.join(ROOT, "apps", "chatgpt_redacted_action.py"),
    }
    try:
        src = {k: open(v).read() for k, v in files.items()}
    except Exception as e:
        return False, f"outlook draft-edit files unreadable: {e}"
    checks = [
        ("tool definition", 'make_tool_def("outlook_edit_draft"' in src["tools/outlook_local.py"]),
        ("inspect definition", 'make_tool_def("outlook_inspect_drafts_for_edit"' in src["tools/outlook_local.py"]),
        ("batch definition", 'make_tool_def("outlook_batch_edit_drafts"' in src["tools/outlook_local.py"]),
        ("tool handler", 'tool_name == "outlook_edit_draft"' in src["tools/outlook_local.py"]),
        ("inspect handler", 'tool_name == "outlook_inspect_drafts_for_edit"' in src["tools/outlook_local.py"]),
        ("batch handler", 'tool_name == "outlook_batch_edit_drafts"' in src["tools/outlook_local.py"]),
        ("owa edit action", "action === 'edit'" in src["tools/owa_write.js"]),
        ("owa inspect action", "action === 'inspect'" in src["tools/owa_write.js"]),
        ("owa batch action", "action === 'batch-edit'" in src["tools/owa_write.js"]),
        ("direct-safe allowlist", '"outlook_edit_draft"' in src["apps/chatgpt_redacted_action.py"]),
        ("inspect allowlist", '"outlook_inspect_drafts_for_edit"' in src["apps/chatgpt_redacted_action.py"]),
        ("batch allowlist", '"outlook_batch_edit_drafts"' in src["apps/chatgpt_redacted_action.py"]),
        ("rewrite safe allowlist", '"outlook_rewrite_drafts_batch"' in src["apps/chatgpt_redacted_action.py"]),
        ("inspect endpoint", "operation_id=\"inspectOutlookDraftsForEdit\"" in src["apps/chatgpt_redacted_action.py"]),
        ("batch endpoint", "operation_id=\"editOutlookDraftsBatch\"" in src["apps/chatgpt_redacted_action.py"]),
        ("atomic rewrite endpoint", "operation_id=\"rewriteOutlookDraftsBatch\"" in src["apps/chatgpt_redacted_action.py"]),
        ("gpt import schema endpoint", '"/openapi-gpt.json"' in src["apps/chatgpt_redacted_action.py"] and "_gpt_import_openapi_schema" in src["apps/chatgpt_redacted_action.py"]),
        ("atomic rewrite helper", "_rewrite_outlook_drafts_batch" in src["apps/chatgpt_redacted_action.py"]),
        ("atomic rewrite completion", "ATOMIC_REWRITE_BATCH_COMPLETE" in src["apps/chatgpt_redacted_action.py"]),
        ("atomic rewrite run alias", 'tool_name in {"outlook_rewrite_drafts_batch", "rewriteOutlookDraftsBatch"}' in src["apps/chatgpt_redacted_action.py"]),
        ("draft inspect cache", "_cache_draft_inspection" in src["apps/chatgpt_redacted_action.py"]),
        ("empty batch recovery", "EMPTY_BATCH_RECOVERED_FROM_LAST_DRAFT_INSPECTION" in src["apps/chatgpt_redacted_action.py"]),
        ("filesystem override", "_bad_fs_for_outlook" in src["apps/chatgpt_redacted_action.py"]),
        ("filesystem outlook gate", "if (not _is_outlook_req) and (_fs or _fs_read or _fs_listd)" in src["apps/chatgpt_redacted_action.py"]),
        ("early inspect execution", 'if tool_name == "outlook_inspect_drafts_for_edit"' in src["apps/chatgpt_redacted_action.py"]),
        ("inspect subject autofill", 'tool_name == "outlook_inspect_drafts_for_edit"' in src["apps/chatgpt_redacted_action.py"] and '_draft_subjects_from_request(body.request_text' in src["apps/chatgpt_redacted_action.py"]),
        ("request router", "OUTLOOK DRAFT EDIT INTERCEPT" in src["apps/chatgpt_redacted_action.py"]),
        ("explicit action endpoint", "operation_id=\"editOutlookDraft\"" in src["apps/chatgpt_redacted_action.py"]),
        ("setup endpoint hidden", re.search(r"_PUBLIC_OPERATION_IDS\s*=\s*\{", src["apps/chatgpt_redacted_action.py"]) is not None and 'operation.get("operationId") not in _PUBLIC_OPERATION_IDS' in src["apps/chatgpt_redacted_action.py"]),
        ("schema guidance", "never say you need to load routing first" in src["apps/chatgpt_redacted_action.py"].lower()),
    ]
    missing = [name for name, ok in checks if not ok]
    return (not missing), ("outlook_edit_draft registered + routed" if not missing else f"DRIFT missing: {missing}")

def fix_outlook_edit_draft_registered():
    return "ALERT-ONLY: outlook_edit_draft missing — restore tools/outlook_local.py + tools/owa_write.js + chatgpt_redacted_action.py"

EMAIL_SENDER_JOBS = [
    "com.claude-stack.revenue-autopilot",
    "com.claude-stack.broken-site-outreach",
    "com.claude-stack.broken-site-rescue",
    "com.claude-stack.reengage-sender",
    "com.claude-stack.BrandA-sender",
    "com.claude-stack.rescue-sender",
]

def chk_email_senders_loaded():
    # Protects the outbound revenue machine from a silent unload (a confused background
    # process halted all of these 2026-06-20). Only enforce jobs whose plist actually exists.
    rc, out = sh(["launchctl", "list"])
    if rc != 0:
        return False, "launchctl list failed"
    missing = [j for j in EMAIL_SENDER_JOBS
               if j not in out and os.path.exists(os.path.join(LA, j + ".plist"))]
    return (not missing), ("email senders loaded" if not missing else f"DRIFT unloaded: {missing}")

def fix_email_senders_loaded():
    reloaded = []
    for j in EMAIL_SENDER_JOBS:
        pf = os.path.join(LA, j + ".plist")
        if not os.path.exists(pf):
            continue
        _rc, out = sh(["launchctl", "list"])
        if j not in out:
            sh(["launchctl", "load", "-w", pf])
            reloaded.append(j)
    return f"reloaded unloaded email senders: {reloaded}" if reloaded else "email senders already loaded"

GOVERNOR_JOBS = [
    "com.claude-stack.remote-priority-governor",
]

def chk_governor_loaded():
    # The remote-priority-governor keeps automation off the P-cores the screen/remote
    # stream needs. It has no KeepAlive (fast-exit one-shot), so a silent unload would
    # leave the recurring remote-freeze protection off with no recovery.
    rc, out = sh(["launchctl", "list"])
    if rc != 0:
        return False, "launchctl list failed"
    missing = [j for j in GOVERNOR_JOBS
               if j not in out and os.path.exists(os.path.join(LA, j + ".plist"))]
    return (not missing), ("priority governor loaded" if not missing else f"DRIFT unloaded: {missing}")

def fix_governor_loaded():
    reloaded = []
    for j in GOVERNOR_JOBS:
        pf = os.path.join(LA, j + ".plist")
        if not os.path.exists(pf):
            continue
        _rc, out = sh(["launchctl", "list"])
        if j not in out:
            sh(["launchctl", "load", "-w", pf])
            reloaded.append(j)
    return f"reloaded unloaded governor: {reloaded}" if reloaded else "priority governor already loaded"

WATCHDOG_JOBS = [
    # The public-surface health watchdog is itself a single point of failure: a watchdog that can
    # silently die is the same trap it exists to prevent. Its own plist has RunAtLoad+KeepAlive
    # (launchd restarts it on crash); this closes the remaining hole — a manual/accidental
    # `launchctl bootout` — by reloading it whenever it is found unloaded.
    "com.claude-stack.public-surface-watchdog",
    # ops-health is the outcome-verify self-heal net for every social/ops lane. It got booted out
    # during launchd churn 2026-07-11 and sat dead 88min (StartInterval jobs have no KeepAlive to
    # respawn them) — the same silent-death trap, one level up. Reload it whenever found unloaded.
    "com.claude-stack.ops-health",
]

def chk_public_surface_watchdog_loaded():
    rc, out = sh(["launchctl", "list"])
    if rc != 0:
        return False, "launchctl list failed"
    missing = [j for j in WATCHDOG_JOBS
               if j not in out and os.path.exists(os.path.join(LA, j + ".plist"))]
    return (not missing), ("public-surface watchdog loaded" if not missing else f"DRIFT unloaded: {missing}")

def fix_public_surface_watchdog_loaded():
    reloaded = []
    for j in WATCHDOG_JOBS:
        pf = os.path.join(LA, j + ".plist")
        if not os.path.exists(pf):
            continue
        _rc, out = sh(["launchctl", "list"])
        if j not in out:
            sh(["launchctl", "load", "-w", pf])
            reloaded.append(j)
    return f"reloaded unloaded watchdog: {reloaded}" if reloaded else "public-surface watchdog already loaded"

AUTONOMY_JOBS = [
    # The always-on autonomous revenue loop. KeepAlive=true normally respawns it, but a fast
    # crash-loop during a high-load boot storm (observed: a Playwright EPIPE under load1>15 right
    # after a 2026-06-30 power failure) makes launchd give up and drop the job out of the domain
    # entirely — it goes silently dark with no recovery. This reloads it whenever found missing.
    "com.claude-stack.revenue-demon",
]

def chk_autonomy_jobs_loaded():
    rc, out = sh(["launchctl", "list"])
    if rc != 0:
        return False, "launchctl list failed"
    missing = [j for j in AUTONOMY_JOBS
               if j not in out and os.path.exists(os.path.join(LA, j + ".plist"))]
    return (not missing), ("autonomy loops loaded" if not missing else f"DRIFT unloaded: {missing}")

def fix_autonomy_jobs_loaded():
    reloaded = []
    for j in AUTONOMY_JOBS:
        pf = os.path.join(LA, j + ".plist")
        if not os.path.exists(pf):
            continue
        _rc, out = sh(["launchctl", "list"])
        if j not in out:
            sh(["launchctl", "load", "-w", pf])
            reloaded.append(j)
    return f"reloaded unloaded autonomy loops: {reloaded}" if reloaded else "autonomy loops already loaded"

OLLAMA_BIN = next((p for p in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
                               "/Applications/Ollama.app/Contents/Resources/ollama") if os.path.exists(p)), "ollama")
EMBED_MODELS = ["nomic-embed-text"]  # openclaw memory index embeds with this; if missing the index 404s and memory search goes dark

def chk_embed_model_present():
    # Only act when ollama is actually up — don't fight a restarting daemon (not this guard's job).
    st, body = http_get("http://127.0.0.1:11434/api/tags", timeout=6)
    if st != 200:
        return True, "ollama not reachable (skip)"
    missing = [m for m in EMBED_MODELS if m not in body]
    return (not missing), ("embed model present" if not missing else f"DRIFT missing ollama model: {missing}")

def fix_embed_model_present():
    pulled = []
    for m in EMBED_MODELS:
        st, body = http_get("http://127.0.0.1:11434/api/tags", timeout=6)
        if st == 200 and m not in body:
            sh([OLLAMA_BIN, "pull", m], timeout=300)
            pulled.append(m)
    return f"pulled missing ollama embed model(s): {pulled}" if pulled else "embed model already present"

# ── Zoom reply-loop self-heal (set-and-forget) ───────────────────────────────────────────────────
# The zoom draft/tee loop is two StartInterval jobs (no KeepAlive to respawn a stall): the READER
# (zoom-web-sync, 150s — reads chat into the cache + keeps the headless web client alive) and the
# DRAFTER (zoom-chat-draft, 180s — scans the cache, drafts, tees, cleans up stale drafts). This
# invariant keeps the loop self-sustaining with ZERO babysitting except the one irreducible human
# touch: a ~1-min re-login when the web session expires (that pages separately, below). It silently:
#   - reloads either job if it got booted out of launchd,
#   - kickstarts the DRAFTER if it hasn't completed a successful run in >15 min (heartbeat = the
#     drafts.json review-queue mtime, rewritten every run),
#   - relaunches the headless Zoom web client + kickstarts the READER if the client is dead AND the
#     cache has gone stale (reader not recovering on its own).
ZOOM_JOBS = ["com.claude-stack.zoom-web-sync", "com.claude-stack.zoom-chat-draft"]
ZOOM_DRAFTER_JOB = "com.claude-stack.zoom-chat-draft"
ZOOM_READER_JOB = "com.claude-stack.zoom-web-sync"
ZOOM_DRAFTS_HB = os.path.join(ROOT, "data", "runtime", "zoom_chat_drafts.json")
ZOOM_CACHE_FILE = os.path.expanduser("~/.openclaw/workspace/CompanyA-local/digests/ZOOM_TEAM_CHAT_THREADS.json")
ZOOM_CDP_PORT = 18810
ZOOM_PROFILE = os.path.expanduser("~/.chrome-cdp-profile-zoomweb")
ZOOM_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ZOOM_DRAFTER_STALL_MIN = 15
ZOOM_READER_STALL_MIN = 8      # reader runs every 150s; >8min unrefreshed cache = reader not working
ZOOM_STALE_HOURS = float(os.environ.get("ZOOM_STALE_HOURS", "6"))   # session-rot threshold (matches sync script)


def _mtime_age_min(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 60.0
    except Exception:
        return 1e9    # missing/unreadable heartbeat -> treat as very stale


def _zoom_client_up():
    rc, out = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "3",
                  f"http://127.0.0.1:{ZOOM_CDP_PORT}/json/version"])
    return out.strip() == "200"


def _zoom_unloaded_jobs():
    rc, out = sh(["launchctl", "list"])
    if rc != 0:
        return []
    return [j for j in ZOOM_JOBS if j not in out and os.path.exists(os.path.join(LA, j + ".plist"))]


def _kickstart(job):
    sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{job}"])


def chk_zoom_reply_loop_healthy():
    problems = []
    unloaded = _zoom_unloaded_jobs()
    if unloaded:
        problems.append(f"job(s) unloaded: {unloaded}")
    draft_age = _mtime_age_min(ZOOM_DRAFTS_HB)
    if draft_age > ZOOM_DRAFTER_STALL_MIN:
        problems.append(f"drafter stalled ({draft_age:.0f}m since last run)")
    cache_age = _mtime_age_min(ZOOM_CACHE_FILE)
    if not _zoom_client_up() and cache_age > ZOOM_READER_STALL_MIN:
        problems.append(f"headless client down + reader not recovering (cache {cache_age:.0f}m stale)")
    return (not problems), ("zoom reply loop healthy" if not problems else "; ".join(problems))


def fix_zoom_reply_loop():
    acted = []
    for j in _zoom_unloaded_jobs():
        sh(["launchctl", "load", "-w", os.path.join(LA, j + ".plist")])
        acted.append(f"reloaded {j}")
    if _mtime_age_min(ZOOM_DRAFTS_HB) > ZOOM_DRAFTER_STALL_MIN:
        _kickstart(ZOOM_DRAFTER_JOB)
        acted.append("kickstarted stalled drafter")
    if not _zoom_client_up() and _mtime_age_min(ZOOM_CACHE_FILE) > ZOOM_READER_STALL_MIN:
        # relaunch the headless web client the reader shares, then force the reader to re-warm + read
        subprocess.Popen(
            [ZOOM_CHROME, f"--user-data-dir={ZOOM_PROFILE}", f"--remote-debugging-port={ZOOM_CDP_PORT}",
             "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
             "https://app.zoom.us/wc/team-chat"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _kickstart(ZOOM_READER_JOB)
        acted.append("relaunched headless client + kickstarted reader")
    return ("; ".join(acted)) if acted else "zoom reply loop already healthy"


# Session-expired = the ONE irreducible human touch (a ~1-min re-login). Detect the silently-rotted
# session (cache rewritten every run but its newest message is hours old — the sync script's own
# stale detector, backstopped here so it still pages even if the reader JOB itself is dead) and PAGE
# Operator. Same issue-key as the sync script -> alert_gateway dedups to ONE page, never a double-buzz.
def _zoom_cache_newest_age_h():
    try:
        d = json.load(open(ZOOM_CACHE_FILE))
    except Exception:
        return None      # no cache -> the 0/0 path (owned by the sync script) handles the empty case
    ts = [v.get("lastT") for v in d.values() if isinstance(v, dict) and isinstance(v.get("lastT"), (int, float))]
    if not ts:
        return None
    mx = max(ts)
    mx = mx / 1000 if mx > 1e12 else mx
    return (time.time() - mx) / 3600.0


def chk_zoom_session_live():
    age_h = _zoom_cache_newest_age_h()
    if age_h is None:
        return True, "zoom session (no cache datapoint — sync script owns the empty case)"
    if age_h > ZOOM_STALE_HOURS:
        return False, f"SESSION EXPIRED: newest zoom msg {age_h:.1f}h old (>{ZOOM_STALE_HOURS}h) — needs a ~1-min re-login"
    return True, f"zoom session live (newest msg {age_h:.1f}h old)"


def fix_zoom_session_live():
    age_h = _zoom_cache_newest_age_h()
    if age_h is None or age_h <= ZOOM_STALE_HOURS:
        return "zoom session live"
    try:
        sys.path.insert(0, ROOT)
        from core import alert_gateway
        alert_gateway.notify(
            "zoom-web-bridge", "Zoom chat session needs re-login",
            f"Zoom web session appears expired — newest message is {age_h:.1f}h old (threshold "
            f"{ZOOM_STALE_HOURS}h). Relaunch ~/.chrome-cdp-profile-zoomweb visibly, log in once, then quit.",
            issue_key="zoom-web-stale", level="warn")
    except Exception as e:
        return f"ALERT (session expired) — page attempt failed: {e}"
    return "ALERT: paged Operator to re-login the Zoom web session (irreducible ~1-min human touch)"


def chk_no_headful_steal():
    """No autonomous code opens a headful Chrome that can steal the macOS
    foreground/cursor — every Playwright launch routes through
    core.chrome_launch (interactive TTY-gated logins marked # headful-ok)."""
    scanner = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tools", "check_no_headful_steal.py")
    try:
        r = subprocess.run([sys.executable, scanner], capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, f"scanner failed to run: {e}"
    if r.returncode == 0:
        return True, "no unguarded headful launches"
    bad = [l.strip() for l in r.stdout.split("\n") if ".py:" in l]
    return False, f"{len(bad)} raw headful launch(es): {bad[:3]}"


def fix_no_headful_steal():
    return ("manual: route the flagged launch(es) through "
            "core.chrome_launch.safe_launch/resolve_launch (see scanner output)")


INVARIANTS = [
    {"id": "no_headful_foreground_steal", "desc": "No autonomous headful Chrome steals the macOS foreground/cursor (all launches via core.chrome_launch)",
     "check": chk_no_headful_steal, "fix": fix_no_headful_steal, "auto": False},
    {"id": "zoom_reply_loop_healthy", "desc": "Zoom draft/tee loop self-sustains (reader+drafter loaded, drafter not stalled, dead client relaunched)",
     "check": chk_zoom_reply_loop_healthy, "fix": fix_zoom_reply_loop, "auto": True},
    {"id": "zoom_session_live", "desc": "Zoom web session not silently rotted; page Operator for the one irreducible re-login",
     "check": chk_zoom_session_live, "fix": fix_zoom_session_live, "auto": True},
    {"id": "workgpt_base_redacted", "desc": "Work GPT base URL = work.Operator.com (never docsapp)",
     "check": chk_workgpt_base, "fix": fix_workgpt_base, "auto": True},
    {"id": "sf_no_autonomous_writes", "desc": "sf-agent never autonomously writes to Salesforce",
     "check": chk_sf_no_autonomous_writes, "fix": fix_sf_no_autonomous_writes, "auto": True},
    {"id": "onedrive_ownership_guard", "desc": "OneDrive sync can only write to Operator's own space",
     "check": chk_onedrive_guard, "fix": fix_onedrive_guard, "auto": True},
    {"id": "firewall_remote_viewing", "desc": "Firewall never blocks remote viewing",
     "check": chk_firewall_remote_viewing, "fix": fix_firewall_remote_viewing, "auto": True},
    {"id": "agents_governing_rules", "desc": "AGENTS.md holds persona + read-only + domain rules",
     "check": chk_agents_rules_present, "fix": fix_agents_rules_present, "auto": False},
    {"id": "outlook_edit_draft_registered", "desc": "Work-GPT can edit existing Outlook Drafts in place",
     "check": chk_outlook_edit_draft_registered, "fix": fix_outlook_edit_draft_registered, "auto": False},
    {"id": "corp_writes_gated", "desc": "Every SF/Monday admin write requires explicit approval (no ungated prod writes)",
     "check": chk_corp_writes_gated, "fix": fix_corp_writes_gated, "auto": False},
    {"id": "email_senders_loaded", "desc": "Outbound revenue senders stay loaded (auto-heal a rogue/accidental unload)",
     "check": chk_email_senders_loaded, "fix": fix_email_senders_loaded, "auto": True},
    {"id": "remote_priority_governor_loaded", "desc": "CPU/QoS governor stays loaded so the remote session can't be starved (auto-heal an unload)",
     "check": chk_governor_loaded, "fix": fix_governor_loaded, "auto": True},
    {"id": "public_surface_watchdog_loaded", "desc": "Public-surface health watchdog stays loaded (a watchdog that can silently die is the same trap)",
     "check": chk_public_surface_watchdog_loaded, "fix": fix_public_surface_watchdog_loaded, "auto": True},
    {"id": "autonomy_loops_loaded", "desc": "Always-on autonomous revenue loop (revenue-demon) stays loaded; auto-heal a boot-storm crash-out launchd gave up on",
     "check": chk_autonomy_jobs_loaded, "fix": fix_autonomy_jobs_loaded, "auto": True},
    {"id": "embed_model_present", "desc": "ollama embed model (nomic-embed-text) present so openclaw memory index never silently 404s",
     "check": chk_embed_model_present, "fix": fix_embed_model_present, "auto": True},
]

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}

def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=2)

def log_changelog(lines):
    try:
        with open(CHANGELOG, "a") as f:
            f.write("\n## %s — invariants_guard\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            for l in lines:
                f.write("- %s\n" % l)
    except Exception:
        pass

def alert(msg):
    # Route through the ONE alert gateway (dedup + digest), NOT a direct iMessage sender.
    # ALERT-ONLY invariants re-emit their string every run, so without dedup this spams the
    # phone (it did). A guard-drift is not revenue/deals/emergency -> warn level = coalesced
    # into the 15-min digest, and issue_key dedups identical repeats to one line per cooldown.
    try:
        sys.path.insert(0, ROOT)
        import hashlib
        from core import alert_gateway
        ik = "invguard-" + hashlib.sha256(msg.encode("utf-8")).hexdigest()[:12]
        alert_gateway.notify("invariants_guard", "🔒 invariants guard", msg, issue_key=ik, level="warn")
    except Exception:
        try:
            from tools.imsg_bridge import send_imessage
            send_imessage(msg)
        except Exception as e:
            sys.stderr.write(f"[invguard] alert failed: {e}\n")

def main():
    prev = load_state()
    results, changes, fixes = {}, [], []
    for inv in INVARIANTS:
        ok, detail = inv["check"]()
        action = ""
        if not ok and DOING_FIX:
            try:
                action = inv["fix"]() if inv["auto"] else inv["fix"]()
                fixes.append(f"{inv['id']}: {detail} -> FIX: {action}")
                ok2, detail2 = inv["check"]()
                detail = f"{detail} | after-fix: {'OK' if ok2 else detail2}"
                ok = ok2 or action.startswith(("NEUTRALIZED", "ALERT"))
            except Exception as e:
                action = f"fix error: {e}"
                fixes.append(f"{inv['id']}: fix raised {e}")
        results[inv["id"]] = {"ok": ok, "detail": detail, "action": action}
        # detect state change vs last run
        was = (prev.get(inv["id"]) or {}).get("ok")
        if was is True and not ok:
            changes.append(f"⚠️ DRIFT {inv['id']}: {detail}")
        if action:
            changes.append(f"🔧 {inv['id']}: {action}")

    save_state(results)
    bad = [k for k, v in results.items() if not v["ok"]]
    print(f"[invguard] {len(INVARIANTS)-len(bad)}/{len(INVARIANTS)} ok" + (f" | issues: {bad}" if bad else ""))
    for k, v in results.items():
        print(f"  {'✓' if v['ok'] else '✗'} {k}: {v['detail']}" + (f"  [{v['action']}]" if v["action"] else ""))
    if changes or fixes:
        log_changelog(changes + fixes)
        if changes:
            alert("🔒 invariants guard:\n" + "\n".join(changes[:6]))

if __name__ == "__main__":
    main()
