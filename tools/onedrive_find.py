#!/usr/bin/env python3
"""onedrive_find — on-demand search of my OneDrive / SharePoint work files. Runs ONLY when I
ask (e.g. "find this in onedrive"); nothing scheduled. Searches filenames across the mounted
OneDrive + SharePoint libraries instantly, ranks by relevance + recency, and (with --read) pulls
and prints the text of the top matches.

  onedrive_find.py "dpp reconciliation"            # list ranked matches (names only, no download)
  onedrive_find.py "tavant uat" --read             # also read+print top 3 matches' text
  onedrive_find.py "weekly report" --read -n 5      # read top 5
  onedrive_find.py --inventory                      # one-time orientation: folders + recent files
"""
import os, sys, subprocess, re, time, html

HOME = os.path.expanduser("~")
ROOTS = [
    os.path.join(HOME, "Library/CloudStorage/OneDrive-redactedIndustries,Inc"),
    os.path.join(HOME, "Library/CloudStorage/OneDrive-SharedLibraries-redactedIndustries,Inc"),
    os.path.join(HOME, "Library/CloudStorage/user@example.com"),
]
SKIP_DIRS = {".git", "node_modules", "Icon\r"}
TEXT_EXT = {".md", ".txt", ".csv", ".json", ".html", ".htm", ".log"}

def walk_files():
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        label = root.split("/")[-1]
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for fn in fns:
                if fn.startswith("."):
                    continue
                yield label, os.path.join(dp, fn), fn

def score(query, name, mtime):
    q = query.lower().strip()
    n = name.lower()
    s = 0
    if q == os.path.splitext(n)[0]: s += 100
    if q in n: s += 50
    toks = [t for t in re.split(r"\W+", q) if t]
    s += 12 * sum(1 for t in toks if t in n)
    # recency nudge (newer = higher), capped
    age_days = max(0, (time.time() - mtime) / 86400)
    s += max(0, 20 - age_days / 30)
    return s

def read_text(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in TEXT_EXT:
            txt = open(path, encoding="utf-8", errors="ignore").read()
            if ext in (".html", ".htm"):
                txt = re.sub(r"<[^>]+>", " ", txt)
                txt = html.unescape(txt)
            return re.sub(r"\n{3,}", "\n\n", txt).strip()
        if ext == ".docx":
            # extract document text without external libs (unzip the xml, strip tags)
            out = subprocess.run(["unzip", "-p", path, "word/document.xml"],
                                 capture_output=True, timeout=30)
            xml = out.stdout.decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"<[^>]+>", "", xml)
            return html.unescape(xml).strip()
        return f"[{ext or 'no-ext'} file — {os.path.getsize(path)} bytes; open in its app to view]"
    except Exception as e:
        return f"[could not read: {e}]"

def inventory():
    vd = ROOTS[0]
    print("# OneDrive orientation\n")
    print("## Top-level folders")
    for item in sorted(os.listdir(vd)):
        p = os.path.join(vd, item)
        if os.path.isdir(p):
            try: n = sum(len(f) for _, _, f in os.walk(p))
            except Exception: n = "?"
            print(f"- **{item}/** ({n} files)")
    rows = []
    for label, path, fn in walk_files():
        try: rows.append((os.path.getmtime(path), label, path, fn))
        except Exception: pass
    rows.sort(reverse=True)
    print("\n## 25 most recently modified files")
    for mtime, label, path, fn in rows[:25]:
        when = time.strftime("%Y-%m-%d", time.localtime(mtime))
        rel = path.split(label + "/", 1)[-1]
        print(f"- {when}  {rel}")

def main():
    args = sys.argv[1:]
    if "--inventory" in args:
        inventory(); return
    read = "--read" in args
    n = 3
    if "-n" in args:
        try: n = int(args[args.index("-n") + 1])
        except Exception: pass
    query = " ".join(a for a in args if not a.startswith("-") and not a.isdigit())
    if not query:
        print(__doc__); return
    hits = []
    for label, path, fn in walk_files():
        try: mt = os.path.getmtime(path)
        except Exception: mt = 0
        sc = score(query, fn, mt)
        if sc > 10:
            hits.append((sc, mt, label, path, fn))
    hits.sort(reverse=True)
    if not hits:
        print(f"no filename matches for '{query}'. (searched {sum(1 for _ in walk_files())} files)")
        return
    print(f"# matches for '{query}' (top {min(len(hits), 15)})\n")
    for sc, mt, label, path, fn in hits[:15]:
        when = time.strftime("%Y-%m-%d", time.localtime(mt)) if mt else "?"
        rel = path.split(label + "/", 1)[-1]
        print(f"- [{when}] {rel}")
    if read:
        print(f"\n--- reading top {n} ---")
        for sc, mt, label, path, fn in hits[:n]:
            print(f"\n## {fn}\n")
            print(read_text(path)[:6000])

if __name__ == "__main__":
    main()
