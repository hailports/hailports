"""Generate the hailports per-vertical stock-photo bank locally ($0, 100% ours).

SD-Turbo on MPS -> photorealistic trade scene photos, cached as JPEGs + a manifest. These
feed the rebuild mockups (hero/gallery) when a prospect is logo-only or photo-less, and are
the ONLY imagery used in public/anonymized demos (no real-customer photos). Scene-first
prompts (not close-up people) to minimize AI-hand/face artifacts.

  python3 tools/gen_stock_imagery.py            # generate the whole bank
  python3 tools/gen_stock_imagery.py plumber    # one vertical
"""
import sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "hustle" / "stock_imagery"
N_PER = 7   # hero + 6 distinct gallery shots => no visible repetition

PROMPTS = {
    "plumber": "clean modern bathroom with new chrome fixtures and a pedestal sink, professional plumbing, photorealistic, bright natural light",
    "hvac": "technician servicing a residential outdoor air-conditioning condenser unit beside a house, photorealistic, sunny day",
    "restaurant": "warm inviting restaurant interior with set wooden tables and soft pendant lighting, photorealistic",
    "salon": "modern hair salon interior with styling chairs and large mirrors, clean and bright, photorealistic",
    "landscaping": "beautifully landscaped suburban front yard, lush green lawn, trimmed hedges, stone path, photorealistic",
    "dentist": "clean modern dental office operatory room, professional equipment, bright and calm, photorealistic",
    "roofing": "brand new asphalt shingle roof on a suburban house against a clear blue sky, photorealistic",
    "pest": "clean bright home interior baseboard closeup with a pest-control sprayer, photorealistic",
    "electrician": "neatly wired residential electrical panel, professional installation, photorealistic, well lit",
    "generic": "modern small-business storefront on a friendly main street, welcoming entrance, photorealistic",
}


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    which = argv[1:] or list(PROMPTS)
    import torch
    from diffusers import AutoPipelineForText2Image
    pipe = AutoPipelineForText2Image.from_pretrained("stabilityai/sd-turbo", torch_dtype=torch.float16)
    pipe = pipe.to("mps"); pipe.set_progress_bar_config(disable=True)
    manifest = {}
    if (OUT / "manifest.json").exists():
        manifest = json.loads((OUT / "manifest.json").read_text())
    for v in which:
        p = PROMPTS.get(v)
        if not p:
            print(f"skip unknown vertical {v}", flush=True); continue
        files = []
        for i in range(N_PER):
            # vary the seed by index so the 3 shots differ (Math.random-free determinism)
            g = torch.Generator(device="mps").manual_seed(sum(ord(c) for c in v) * 1000 + i)
            img = pipe(prompt=p, num_inference_steps=3, guidance_scale=0.0,
                       height=512, width=768, generator=g).images[0]
            fn = OUT / f"{v}_{i}.jpg"
            img.convert("RGB").save(fn, format="JPEG", quality=86, optimize=True)
            files.append(fn.name)
            print(f"[{time.strftime('%H:%M:%S')}] {fn.name}", flush=True)
        manifest[v] = files
        (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("DONE", len(manifest), "verticals", flush=True)


if __name__ == "__main__":
    main(sys.argv)
