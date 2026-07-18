"""Replace specific year/org/user claims with certification-forward language across all files."""
import os
from pathlib import Path

BASE = Path.home() / "claude-stack"

REPLACEMENTS = [
    # Specific year claims
    ("multiple Salesforce certifications across enterprise orgs", "multiple Salesforce certifications across enterprise orgs"),
    ("multi-certified · enterprise orgs", "multi-certified · enterprise orgs"),
    ("multiple Salesforce certifications across", "multiple Salesforce certifications across"),
    ("multiple certifications across", "multiple certifications across"),
    ("multi-certified", "multi-certified"),
    ("multiple Salesforce certifications, enterprise orgs", "multiple Salesforce certifications, enterprise orgs"),
    ("multiple Salesforce certifications, enterprise orgs", "multiple Salesforce certifications, enterprise orgs"),
    ("multi-certified · enterprise-scale deployments", "multi-certified · enterprise-scale deployments"),
    ("multi-certified &middot; enterprise-scale deployments", "multi-certified &middot; enterprise-scale deployments"),
    ("multiple Salesforce certifications and enterprise-scale experience", "multiple Salesforce certifications and enterprise-scale experience"),
    # Specific user/org counts
    ("managed enterprise-scale migrations", "managed enterprise-scale migrations"),
    ("enterprise-scale migrations", "enterprise-scale migrations"),
    ("enterprise orgs across multiple industries", "enterprise orgs across multiple industries"),
    ("enterprise orgs", "enterprise orgs"),
    ("across enterprise", "across enterprise"),
    # 2x Certified framing
    ("Salesforce-certified practitioner", "Salesforce-certified practitioner"),
    ("Salesforce-certified practitioner", "Salesforce-certified practitioner"),
    ("Salesforce-certified practitioner", "Salesforce-certified practitioner"),
    ("Salesforce-certified (multiple certifications)", "Salesforce-certified (multiple certifications)"),
    ("multi-certified", "multi-certified"),
    ("Built by practitioners, not a vendor. Multiple Salesforce certifications. Enterprise-scale experience.",
     "Built by practitioners, not a vendor. Multiple Salesforce certifications. Enterprise-scale experience."),
    ("By Salesforce-certified practitioners · enterprise orgs",
     "By Salesforce-certified practitioners · enterprise orgs"),
    # Proposal prompts
    ("Mention: multiple Salesforce certifications, enterprise org experience",
     "Mention: multiple Salesforce certifications, enterprise org experience"),
    ("Mention multiple Salesforce certifications, enterprise org experience",
     "Mention multiple Salesforce certifications, enterprise org experience"),
    # Blog author voice
    ("Our team has managed enterprise orgs across multiple industries", "Our team has managed enterprise orgs across multiple industries"),
    ("worked across enterprise orgs spanning multiple industries", "worked across enterprise orgs spanning multiple industries"),
    ("practitioner who has managed enterprise orgs across multiple industries", "practitioner who has managed enterprise orgs across multiple industries"),
    ("Across enterprise Salesforce orgs", "Across enterprise Salesforce orgs"),
    ("In our experience,", "In our experience,"),
    ("We've seen these limits cripple enterprise deployments",
     "We've seen these limits cripple enterprise deployments"),
]

TARGET_EXTS = {'.py', '.html', '.md', '.txt', '.json'}
SKIP_DIRS = {'__pycache__', '.git', 'node_modules', 'manual_backups'}

changed_files = []

for root, dirs, files in os.walk(BASE):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for fname in files:
        if not any(fname.endswith(ext) for ext in TARGET_EXTS):
            continue
        fpath = Path(root) / fname
        try:
            content = fpath.read_text(errors='replace')
            original = content
            for old, new in REPLACEMENTS:
                content = content.replace(old, new)
            if content != original:
                fpath.write_text(content)
                changed_files.append(str(fpath.relative_to(BASE)))
        except Exception as e:
            print(f"  skip {fpath}: {e}")

print(f"Fixed {len(changed_files)} files:")
for f in changed_files:
    print(f"  {f}")
