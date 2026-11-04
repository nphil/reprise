// Reprise: GPU verdict for one plane pair of two decoded frames (see recurrence.video_matches).
//
// Original work of this project (MIT, see LICENSE); it includes no NVIDIA header, library or sample code.
// Build: tools/build_reprise_ptx.py compiles it (NVRTC, compute_61, once per SAMPLE_T) into the verdict_u8.ptx and
// verdict_u16.ptx next to it; those are what ships. See cuda_kernels/README.md.
//
// Every thread owns one tile or window and sums the exact integer differences L - R, so the sums have no rounding.
// The CPU reference (recurrence._plane_matches) sums float32 / float64, which differs from the exact value by less
// than the bands below; a metric inside its band is reported as "ambiguous" and the host redoes the plane with the
// CPU code, so a plane is accepted or rejected here only when the CPU would reach the same verdict.
//
//   tile means    float32 sums of at most 64 values: relative error < 66 * 2^-24 < 4e-6      -> band REL = 1e-5
//   signed sums   window sums of float32 residuals in float64: absolute error < 2e-2 (10-bit) -> band ABS = 0.05
//   peak / count  integer thresholds computed on the host with the CPU's own float32 arithmetic: exact
//
// res[0] counts certain violations, res[1] counts metrics inside a band.

extern "C" {

#define REL 1e-5
#define ABS 0.05

// The tile grid recurrence._local_max builds: boundaries 0, [off], off + size, ..., n (np.unique of r_[0, arange(off, n, size), n]).
__device__ __forceinline__ int tile_count(int n, int off, int size)
{
    return off == 0 ? (n + size - 1) / size : 1 + (n - off + size - 1) / size;
}

__device__ __forceinline__ int tile_edge(int k, int n, int off, int size)
{
    if (off == 0) {
        int v = k * size;
        return v < n ? v : n;
    }
    if (k == 0) return 0;
    int v = off + (k - 1) * size;
    return v < n ? v : n;
}

__device__ __forceinline__ int abs_diff(SAMPLE_T a, SAMPLE_T b)
{
    int d = (int)a - (int)b;
    return d < 0 ? -d : d;
}

// Elements whose |L - R| reaches raw_limit (the first integer whose float32 scaled value exceeds the peak limit).
__global__ void peak(const SAMPLE_T* left, const SAMPLE_T* right, int n, int raw_limit, unsigned* res)
{
    unsigned over = 0;
    for (int i = blockIdx.x * blockDim.x + threadIdx.x; i < n; i += gridDim.x * blockDim.x)
        over += abs_diff(left[i], right[i]) >= raw_limit;
    if (over) atomicAdd(&res[0], over);
}

// Mean |L - R| per tile (local4 / local8) and, for 8-pixel tiles, the fraction of elements above the concentration level.
__global__ void tiles(const SAMPLE_T* left, const SAMPLE_T* right, int h, int w, int size, int oy, int ox,
                      double scale, double limit_mean, int level_raw, int with_fraction, double limit_fraction,
                      unsigned* res)
{
    int ny = tile_count(h, oy, size), nx = tile_count(w, ox, size);
    int t = blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= ny * nx) return;
    int ty = t / nx, tx = t % nx;
    int y0 = tile_edge(ty, h, oy, size), y1 = tile_edge(ty + 1, h, oy, size);
    int x0 = tile_edge(tx, w, ox, size), x1 = tile_edge(tx + 1, w, ox, size);
    int sum = 0, count = 0;
    for (int y = y0; y < y1; ++y)
        for (int x = x0; x < x1; ++x) {
            int d = abs_diff(left[y * w + x], right[y * w + x]);
            sum += d;
            count += d >= level_raw;
        }
    double area = (double)((y1 - y0) * (x1 - x0));
    double mean = (double)sum * scale / area;
    if (mean > limit_mean * (1.0 + REL)) atomicAdd(&res[0], 1u);
    else if (mean >= limit_mean * (1.0 - REL)) atomicAdd(&res[1], 1u);
    // The count is an exact integer on both sides, so the fraction compares exactly (a correctly rounded quotient).
    if (with_fraction && (double)count / area > limit_fraction) atomicAdd(&res[0], 1u);
}

// |mean of L - R| over size x size windows at half-window stride from 0 to n - size, plus the far-edge window
// (recurrence._signed_local): the window sum in 8-bit units against limit * size^2.
__global__ void signed_windows(const SAMPLE_T* left, const SAMPLE_T* right, int h, int w, int size, double scale,
                               double limit, unsigned* res)
{
    int stride = size / 2;
    int cy = (h - size) / stride + 1, cx = (w - size) / stride + 1;
    int ny = cy + (((h - size) % stride) != 0), nx = cx + (((w - size) % stride) != 0);
    int t = blockIdx.x * blockDim.x + threadIdx.x;
    if (t >= ny * nx) return;
    int ty = t / nx, tx = t % nx;
    int y0 = ty < cy ? ty * stride : h - size;
    int x0 = tx < cx ? tx * stride : w - size;
    int sum = 0;
    for (int y = y0; y < y0 + size; ++y)
        for (int x = x0; x < x0 + size; ++x)
            sum += (int)left[y * w + x] - (int)right[y * w + x];
    double magnitude = fabs((double)sum) * scale;
    double bound = limit * (double)size * (double)size;
    if (magnitude > bound + ABS) atomicAdd(&res[0], 1u);
    else if (magnitude >= bound - ABS) atomicAdd(&res[1], 1u);
}

}
