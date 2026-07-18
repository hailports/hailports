#!/usr/bin/env python3
"""Vet a local GGUF model before wiring it into the stack.

Two gates:
  1. structural  -- parse the GGUF header/metadata + tensor table, confirm the
     file isn't truncated (all tensor data fits inside the file).
  2. load-test   -- optionally load it through ollama's llama.cpp and run one
     token of inference. This is what catches the failure that structural checks
     miss: a bad vocab (e.g. duplicate tokens) that ggml_abort/GGML_ASSERTs at
     load time. That's the exact bug that crashed TypeAhead on gemma-4-E4B --
     file was byte-perfect, vocab was broken. See ~/.claude memory.

Exit codes: 0 = ok, 1 = structural fail, 2 = load fail, 3 = usage/other.

Usage:
    gguf_check.py MODEL.gguf              # structural + ollama load-test
    gguf_check.py --no-load MODEL.gguf    # structural only (fast, no ollama)
    gguf_check.py --json MODEL.gguf       # machine-readable result
"""
import json
import os
import struct
import subprocess
import sys
import tempfile

GGUF_MAGIC = b"GGUF"


def _read_meta(path):
    """Parse header + KV + tensor table. Returns dict or raises."""
    with open(path, "rb") as f:
        d = f.read
        if d(4) != GGUF_MAGIC:
            raise ValueError("not a GGUF file (bad magic)")
        ver = struct.unpack("<I", d(4))[0]
        n_tensors = struct.unpack("<Q", d(8))[0]
        n_kv = struct.unpack("<Q", d(8))[0]

        def rstr():
            n = struct.unpack("<Q", d(8))[0]
            return d(n).decode("utf-8", "replace")

        def rval(t):
            if t == 0:  return d(1)[0]                       # uint8
            if t == 1:  return struct.unpack("<b", d(1))[0]  # int8
            if t == 2:  return struct.unpack("<H", d(2))[0]  # uint16
            if t == 3:  return struct.unpack("<h", d(2))[0]  # int16
            if t == 4:  return struct.unpack("<I", d(4))[0]  # uint32
            if t == 5:  return struct.unpack("<i", d(4))[0]  # int32
            if t == 6:  return struct.unpack("<f", d(4))[0]  # float32
            if t == 7:  return d(1)[0]                       # bool
            if t == 8:  return rstr()                        # string
            if t == 9:                                       # array
                et = struct.unpack("<I", d(4))[0]
                ln = struct.unpack("<Q", d(8))[0]
                return [rval(et) for _ in range(ln)]
            if t == 10: return struct.unpack("<Q", d(8))[0]  # uint64
            if t == 11: return struct.unpack("<q", d(8))[0]  # int64
            if t == 12: return struct.unpack("<d", d(8))[0]  # float64
            raise ValueError(f"unknown KV value type {t}")

        kv = {}
        for _ in range(n_kv):
            k = rstr()
            t = struct.unpack("<I", d(4))[0]
            v = rval(t)
            # keep scalars + short arrays; skip giant vocab arrays
            if not isinstance(v, list) or len(v) <= 8:
                kv[k] = v

        last_off = 0
        for _ in range(n_tensors):
            rstr()  # tensor name
            nd = struct.unpack("<I", d(4))[0]
            for _ in range(nd):
                struct.unpack("<Q", d(8))[0]  # dim
            struct.unpack("<I", d(4))[0]      # ggml type
            off = struct.unpack("<Q", d(8))[0]
            last_off = max(last_off, off)

        data_start = f.tell()

    align = kv.get("general.alignment", 32) or 32
    if data_start % align:
        data_start += align - (data_start % align)
    fsz = os.path.getsize(path)

    return {
        "gguf_version": ver,
        "n_tensors": n_tensors,
        "n_kv": n_kv,
        "arch": kv.get("general.architecture"),
        "name": kv.get("general.name"),
        "tokenizer": kv.get("tokenizer.ggml.model"),
        "file_size": fsz,
        "data_start": data_start,
        "last_tensor_offset": last_off,
        # data region must at least reach the last tensor's start offset
        "truncated": fsz < data_start + last_off,
        "slack_bytes": fsz - (data_start + last_off),
    }


def _load_test(path):
    """Load via ollama + one inference token. Returns (ok, detail)."""
    if not _which("ollama"):
        return None, "ollama not found; skipping load-test"
    tag = "gguf-check-" + str(os.getpid())
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False) as mf:
        mf.write(f"FROM {os.path.abspath(path)}\n")
        mfpath = mf.name
    try:
        c = subprocess.run(["ollama", "create", tag, "-f", mfpath],
                           capture_output=True, text=True, timeout=600)
        if c.returncode != 0:
            return False, "ollama create failed: " + _tail(c.stderr or c.stdout)
        r = subprocess.run(["ollama", "run", tag, "say ok"],
                           capture_output=True, text=True, timeout=300)
        blob = (r.stderr or "") + (r.stdout or "")
        if r.returncode != 0 or "GGML_ASSERT" in blob or "ggml_abort" in blob:
            return False, "inference failed: " + _tail(blob)
        return True, _tail(r.stdout).strip() or "(loaded, empty output)"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    finally:
        subprocess.run(["ollama", "rm", tag], capture_output=True)
        try:
            os.unlink(mfpath)
        except OSError:
            pass


def _which(x):
    return subprocess.run(["which", x], capture_output=True).returncode == 0


def _tail(s, n=400):
    s = (s or "").strip()
    return s[-n:]


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    do_load = "--no-load" not in argv
    as_json = "--json" in argv
    if len(args) != 1:
        print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
        return 3
    path = args[0]
    if not os.path.isfile(path):
        print(f"no such file: {path}", file=sys.stderr)
        return 3

    result = {"path": path}
    try:
        meta = _read_meta(path)
        result.update(meta)
    except Exception as e:  # noqa: BLE001
        result["structural"] = "FAIL"
        result["error"] = str(e)
        _emit(result, as_json)
        return 1

    if meta["truncated"]:
        result["structural"] = "FAIL"
        result["error"] = "file truncated (tensor data past EOF)"
        _emit(result, as_json)
        return 1
    result["structural"] = "OK"

    if do_load:
        ok, detail = _load_test(path)
        result["load_test"] = ("OK" if ok else "SKIP" if ok is None else "FAIL")
        result["load_detail"] = detail
        if ok is False:
            _emit(result, as_json)
            return 2

    _emit(result, as_json)
    return 0


def _emit(r, as_json):
    if as_json:
        print(json.dumps(r, indent=2))
        return
    print(f"file        {r['path']}")
    if "arch" in r:
        gb = r["file_size"] / 1e9
        print(f"arch/tok    {r.get('arch')} / {r.get('tokenizer')}  ({gb:.2f} GB, {r.get('n_tensors')} tensors)")
    print(f"structural  {r['structural']}" + (f"  -- {r['error']}" if r.get("error") else ""))
    if "load_test" in r:
        print(f"load-test   {r['load_test']}  -- {r.get('load_detail','')}")
    verdict = "USABLE" if r.get("structural") == "OK" and r.get("load_test") in (None, "OK", "SKIP") else "REJECT"
    print(f"verdict     {verdict}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
