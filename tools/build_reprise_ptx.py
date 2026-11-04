#!/usr/bin/env python3
"""Build the GPU verdict kernels' PTX from ``cuda_kernels/verdict.cu``.

    python tools/build_reprise_ptx.py --nvrtc /path/to/libnvrtc.so.11.2

The two PTX files next to the source are what ships (the image carries no CUDA toolkit; the driver compiles PTX for the
installed GPU when it loads it, so one build serves every NVIDIA GPU from Pascal on). This needs only NVRTC, the
compiler library, and nothing of NVIDIA's goes into the repository or the image except what NVRTC writes: PTX for our
own source, which includes no NVIDIA header.

Use NVRTC from CUDA 11.8 (``pip install nvidia-cuda-nvrtc-cu11==11.8.89``, ``nvidia/cuda_nvrtc/lib/libnvrtc.so.11.2``):
it writes PTX ISA 7.8, which drivers back to 520 load, and still compiles for ``compute_61``. Newer NVRTC writes a newer
ISA that older drivers refuse. CUDA 13 dropped Pascal; the PTX is forward compatible and runs there on newer GPUs.

Run it again when ``verdict.cu`` changes and commit both PTX files; ``tests/reprise/test_cuda_metrics.py`` fails when
the committed PTX is older than the source's hash recorded in its header comment.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import sys
from pathlib import Path

KERNELS = Path(__file__).resolve().parents[1] / "media_preview_generator" / "reprise" / "cuda_kernels"
VARIANTS = {"verdict_u8.ptx": "unsigned char", "verdict_u16.ptx": "unsigned short"}
ARCHITECTURE = "compute_61"


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def compile_ptx(nvrtc: ctypes.CDLL, source: str, name: str) -> str:
    program = ctypes.c_void_p()
    code = nvrtc.nvrtcCreateProgram(ctypes.byref(program), source.encode(), name.encode(), 0, None, None)
    if code:
        raise SystemExit(f"nvrtcCreateProgram failed ({code})")
    options = (ctypes.c_char_p * 2)(f"--gpu-architecture={ARCHITECTURE}".encode(), b"--fmad=false")
    code = nvrtc.nvrtcCompileProgram(program, len(options), options)
    size = ctypes.c_size_t()
    nvrtc.nvrtcGetProgramLogSize(program, ctypes.byref(size))
    log = ctypes.create_string_buffer(size.value)
    nvrtc.nvrtcGetProgramLog(program, log)
    if code:
        raise SystemExit(f"compile failed ({code}):\n{log.value.decode(errors='replace')}")
    nvrtc.nvrtcGetPTXSize(program, ctypes.byref(size))
    ptx = ctypes.create_string_buffer(size.value)
    nvrtc.nvrtcGetPTX(program, ptx)
    nvrtc.nvrtcDestroyProgram(ctypes.byref(program))
    return ptx.value.decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nvrtc", required=True, help="path to libnvrtc.so (CUDA 11.8)")
    args = parser.parse_args()
    nvrtc = ctypes.CDLL(args.nvrtc)
    source = (KERNELS / "verdict.cu").read_text()
    digest = source_hash(source)
    for name, sample in VARIANTS.items():
        ptx = compile_ptx(nvrtc, f"#define SAMPLE_T {sample}\n{source}", "verdict.cu")
        (KERNELS / name).write_text(f"// verdict.cu sha256 {digest}\n{ptx}")
        print(f"wrote {name} ({len(ptx)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
