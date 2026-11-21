# GPU picture-comparison kernels

`compare.cu` is the CUDA source of the two kernels behind `reprise/cuda_compare.py`, which compares many pairs of
canonical pictures per launch. `compare.ptx` is the same source built for the driver to load.

- `cells_to_picture` turns the 16-bit cell planes of every distinct frame into its float32 canonical picture, the
  numbers `canonical.picture` makes (scale to 8-bit units, and the full-range mapping when the source is full range).
- `pair_metrics` takes one thread block for each pair and plane and computes the seven numbers of
  `recurrence.picture_metrics` for it (peak, local 4 and 8 means, concentration, signed 8/16/32 windows). The host takes
  the worst of the three planes of each pair.

The kernels return numbers, never a verdict: the caller compares them with `recurrence.VIDEO` as it does the CPU's, and
checks again on the CPU any pair that lands close to a limit.

## Why this cannot change a verdict

Every sum is float64 and is added in an order the shape of the picture fixes (the same order on every run and every GPU),
and multiplications and additions are never fused (`--fmad=false`, and the float64 mapping uses the explicit
round-to-nearest operations). For limited-range sources every sample is a multiple of 2^-8 and every sum is exact in
float64, so the numbers equal the CPU's exactly; for full-range sources the order of the additions differs from the
CPU's prefix sums and the numbers agree to about 1e-9 relative, far inside the margin the near-limit re-check allows.
On first use `Session.selftest()` runs both kernels against the CPU on synthetic frames, and a disagreement turns the
GPU off for the process. `tests/reprise/test_cuda_compare.py` runs the session against a fake driver that emulates the
kernels, and with a GPU present (`pytest -m gpu`) checks the real kernels against the CPU, byte for byte for pictures.

## Licence and what ships

The `.cu` file is this project's own work (MIT) and includes no NVIDIA header or library. The `.ptx` file is the
compiler's output for it. Nothing from the CUDA toolkit is in the image: `libcuda.so.1` comes from the host driver
through the NVIDIA container runtime, exactly as for FFmpeg's NVDEC (the container needs the `compute` driver capability
besides `video`: `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`). Without it the CPU path runs, silently.

Both files must be listed under `[tool.setuptools.package-data]` in `pyproject.toml` (`"media_preview_generator.reprise"
= ["cuda_kernels/*.cu", "cuda_kernels/*.ptx"]`): the Docker builder has no `.git`, so nothing else puts the PTX in the
wheel, and a test checks it.

## Rebuilding the PTX

Only when `compare.cu` changes (a test fails if the PTX header's source hash is stale):

```sh
pip install --target /tmp/nvrtc nvidia-cuda-nvrtc-cu11==11.8.89
python tools/build_reprise_ptx.py --nvrtc /tmp/nvrtc/nvidia/cuda_nvrtc/lib/libnvrtc.so.11.2
```

CUDA 11.8's NVRTC writes PTX ISA 7.8 (drivers from 520 load it) and still targets `compute_61` (Pascal, the oldest GPU
worth supporting; the driver JIT-compiles the PTX for newer ones). Newer NVRTC versions write an ISA older drivers
reject.

`REPRISE_CUDA_COMPARE=0` turns the GPU comparison off.
