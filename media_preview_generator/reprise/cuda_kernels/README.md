# GPU frame-pair verdict kernels

`verdict.cu` is the CUDA source of the check `recurrence.video_matches` makes on every pair of decoded frames
(peak, local 4/8 means, concentration, signed 8/16/32 windows). `verdict_u8.ptx` and `verdict_u16.ptx` are the same
source built for 8-bit and 10-bit samples; `reprise/cuda_metrics.py` loads them through `libcuda` and falls back to the
CPU for anything it cannot answer exactly.

## Why this cannot change a verdict

Each thread sums the exact integer differences of one tile or window. The CPU path sums float32 / float64 and its
result differs from the exact one by less than the error bands in the file header, so the kernel answers only when the
exact value is outside the band: a certain pass or a certain fail. Anything inside a band is handed back and the CPU
code decides. The peak and the concentration level are integer thresholds computed on the host with the CPU's own
float32 arithmetic. On first use `cuda_metrics` runs the kernels against the CPU on synthetic planes; a disagreement
turns the GPU off for the process. `tests/reprise/test_cuda_metrics.py` models the kernel in numpy and checks the
verdicts on boundary cases at both bit depths, and with a GPU present checks the real kernels too.

## Licence and what ships

The `.cu` file is this project's own work (MIT) and includes no NVIDIA header or library. The `.ptx` files are the
compiler's output for it. Nothing from the CUDA toolkit is in the image: `libcuda.so.1` comes from the host driver
through the NVIDIA container runtime, exactly as for FFmpeg's NVDEC (the container needs the `compute` driver capability
besides `video`: `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`). Without it the CPU path runs, silently.

## Rebuilding the PTX

Only when `verdict.cu` changes (a test fails if the PTX header's source hash is stale):

```sh
pip install --target /tmp/nvrtc nvidia-cuda-nvrtc-cu11==11.8.89
python tools/build_reprise_ptx.py --nvrtc /tmp/nvrtc/nvidia/cuda_nvrtc/lib/libnvrtc.so.11.2
```

CUDA 11.8's NVRTC writes PTX ISA 7.8 (drivers from 520 load it) and still targets `compute_61` (Pascal, the oldest
GPU worth supporting; the driver JIT-compiles the PTX for newer ones). Newer NVRTC versions write an ISA older drivers
reject.

`REPRISE_CUDA_VERDICT=0` turns the GPU comparison off.
