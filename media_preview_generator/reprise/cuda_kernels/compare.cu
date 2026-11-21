// compare.cu: the batched comparison of canonical pictures on the GPU (Reprise).
//
// This file is the project's own work (MIT licence) and includes no NVIDIA header. NVRTC turns it into compare.ptx
// (tools/build_reprise_ptx.py) and cuda_compare.py loads that through libcuda.
//
// Two kernels:
//   cells_to_picture  a frame's cell means (canonical.Cells, u16: 256 x the value on the 8-bit scale) -> its float32
//                     canonical picture, byte for byte what canonical.picture computes on the CPU.
//   pair_metrics      for every frame pair and plane, the seven numbers of recurrence.picture_metrics: peak, local4,
//                     local8, concentration8, signed8, signed16, signed32 (the worst value over the planes is the
//                     caller's maximum of the three rows written per pair).
//
// Why the numbers are the CPU's and not merely close:
//  * Every operation numpy performs one at a time is an explicit round-to-nearest intrinsic here, and the build passes
//    --fmad=false, so a multiply and an add are never fused.
//  * Sums are float64, added in an order that depends on the picture's shape alone and never on thread scheduling
//    (no atomic touches a float). A limited-range picture holds multiples of 2^-8, so each of those sums is exact in
//    float64 and equals the CPU's integral-image sum bit for bit. Elsewhere the two orders can differ in the last
//    bits (about 1e-9 relative at worst); the caller decides pairs that close to a limit on the CPU.
//  * Maxima are exact whatever order they are taken in.
//
// Picture layout (both kernels): frame f of a buffer is frame_size floats (or u16 cells) at f * frame_size: the luma
// plane (luma_height x luma_width, row-major), then the blue and the red plane (colour_height x colour_width each).

#define NUM_METRICS 7
#define BLOCK_THREADS 256

// ---- kernel 1: cells -> picture ----------------------------------------------------------------------------------

// One thread per sample (grid-stride). `count` samples of `frame_size` per frame; the first `luma_size` of a frame are
// luma. A full-range frame is mapped onto the limited scale exactly as canonical.picture does it, in float64:
// luma 16 + v * (219 / 255), colour 128 + (v - 128) * (224 / 255); the two scales arrive as the host's own doubles.
extern "C" __global__ void cells_to_picture(const unsigned short* __restrict__ cells, float* __restrict__ picture,
                                            unsigned long long count, int frame_size, int luma_size, int full,
                                            double luma_scale, double chroma_scale)
{
    const unsigned long long step = (unsigned long long)gridDim.x * blockDim.x;
    for (unsigned long long i = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x; i < count; i += step) {
        // x / 256 exactly: a power-of-two scale of an integer below 2^16.
        double v = __dmul_rn((double)cells[i], 0.00390625);
        if (full) {
            const int within = (int)(i % (unsigned long long)frame_size);
            if (within < luma_size)
                v = __dadd_rn(16.0, __dmul_rn(v, luma_scale));
            else
                v = __dadd_rn(128.0, __dmul_rn(__dsub_rn(v, 128.0), chroma_scale));
        }
        picture[i] = __double2float_rn(v);
    }
}

// ---- kernel 2: pair metrics --------------------------------------------------------------------------------------

// The largest of a warp's 32 values (all 32 lanes call it).
__device__ __forceinline__ double warp_max(double v)
{
    for (int offset = 16; offset > 0; offset >>= 1) {
        const int low = __shfl_down_sync(0xffffffffu, __double2loint(v), offset);
        const int high = __shfl_down_sync(0xffffffffu, __double2hiint(v), offset);
        v = fmax(v, __hiloint2double(high, low));
    }
    return v;
}

// The index-th tile of one axis of `length` samples, for tiles `size` wide: indexes below `first_phase` are the tiles of
// recurrence._grid(length, size, 0) (whole tiles from 0, the last one partial when the length is not a multiple); the
// rest are those of _grid(length, size, size / 2) (a half tile first, then whole tiles from size / 2).
__device__ __forceinline__ void tile_span(int length, int size, int first_phase, int index, int* begin, int* end)
{
    int first, last;
    if (index < first_phase) {
        first = index * size;
        last = first + size;
    } else {
        const int k = index - first_phase;
        first = k == 0 ? 0 : size / 2 + (k - 1) * size;
        last = k == 0 ? size / 2 : first + size;
    }
    *begin = first;
    *end = last < length ? last : length;
}

// One column of tiles (columns x_begin .. x_end, every row) of tile height SIZE, in both row phases at once: the tile
// means of |a - b| (best of them into *mean_best) and, when COUNT, the fraction of samples above `level`
// (*count_best). The rows are one pass down the plane; each row's sum over the tile's columns is added to the two
// tiles it belongs to (one per row phase), which end where recurrence._grid(height, SIZE, 0 / SIZE / 2) ends them.
template <int SIZE, bool COUNT>
__device__ __forceinline__ void tile_column(const float* a, const float* b, int height, int width, int x_begin,
                                            int x_end, float level, double* mean_best, double* count_best)
{
    const int w = x_end - x_begin;
    double sum_a = 0.0, sum_b = 0.0;  // the tile of the first / second row phase being summed
    int over_a = 0, over_b = 0;
    int begin_a = 0, end_a = SIZE < height ? SIZE : height;
    int begin_b = 0, end_b = SIZE / 2;
    for (int y = 0; y < height; ++y) {
        const float* row_a = a + (long long)y * width + x_begin;
        const float* row_b = b + (long long)y * width + x_begin;
        double row = 0.0;
        int over = 0;
        for (int k = 0; k < w; ++k) {
            const float m = fabsf(__fsub_rn(__ldg(row_a + k), __ldg(row_b + k)));
            row = __dadd_rn(row, (double)m);
            if (COUNT) over += m > level;
        }
        sum_a = __dadd_rn(sum_a, row);
        sum_b = __dadd_rn(sum_b, row);
        over_a += over;
        over_b += over;
        if (y + 1 == end_a) {
            const double area = (double)((end_a - begin_a) * w);
            *mean_best = fmax(*mean_best, __ddiv_rn(sum_a, area));
            if (COUNT) *count_best = fmax(*count_best, __ddiv_rn((double)over_a, area));
            sum_a = 0.0;
            over_a = 0;
            begin_a = end_a;
            end_a = end_a + SIZE < height ? end_a + SIZE : height;
        }
        if (y + 1 == end_b) {
            const double area = (double)((end_b - begin_b) * w);
            *mean_best = fmax(*mean_best, __ddiv_rn(sum_b, area));
            if (COUNT) *count_best = fmax(*count_best, __ddiv_rn((double)over_b, area));
            sum_b = 0.0;
            over_b = 0;
            begin_b = end_b;
            end_b = end_b + SIZE < height ? end_b + SIZE : height;
        }
    }
}

// One column of SIZE x SIZE windows (columns x_begin .. x_begin + SIZE, every row position): the largest |sum of
// a - b| over the windows whose rows start every SIZE / 2 and at the far edge (recurrence._strides). A window is two
// consecutive bands of SIZE / 2 rows added together; the far-edge window (the last SIZE rows) is summed on its own.
// The far edge may repeat a window that starts at a multiple of SIZE / 2: a maximum does not mind.
template <int SIZE>
__device__ __forceinline__ double signed_column(const float* a, const float* b, int height, int width, int x_begin)
{
    const int half = SIZE / 2;
    const int edge_row = height - SIZE;
    double band = 0.0, previous = 0.0, edge = 0.0, best = 0.0;
    bool have_previous = false;
    for (int y = 0; y < height; ++y) {
        const float* row_a = a + (long long)y * width + x_begin;
        const float* row_b = b + (long long)y * width + x_begin;
        double row = 0.0;
#pragma unroll
        for (int k = 0; k < SIZE; ++k) row = __dadd_rn(row, (double)__fsub_rn(__ldg(row_a + k), __ldg(row_b + k)));
        band = __dadd_rn(band, row);
        if (y >= edge_row) edge = __dadd_rn(edge, row);
        if (((y + 1) & (half - 1)) == 0) {
            if (have_previous) best = fmax(best, fabs(__dadd_rn(previous, band)));
            previous = band;
            band = 0.0;
            have_previous = true;
        }
    }
    return fmax(best, fabs(edge));
}

// One block per (pair, plane): blocks 0 .. P-1 are the luma planes of the P pairs, P .. 2P-1 the blue planes and
// 2P .. 3P-1 the red ones (the heavy luma blocks start first). `pairs` holds, for each pair, the picture index in
// `left` and in `right`. Writes out[(pair * 3 + plane) * 7 + metric]: the plane's value of each metric, in the order
// peak, local4, local8, concentration8, signed8, signed16, signed32. Every plane is at least 32 x 32.
//
// The block takes its work as items (a column of tiles or of windows each) from a shared counter, heaviest first, so
// the threads finish together; which thread runs an item changes nothing, since an item's value is computed from its
// own samples in a fixed order and the block takes maxima.
extern "C" __global__ void __launch_bounds__(BLOCK_THREADS)
pair_metrics(const float* __restrict__ left, const float* __restrict__ right, const int* __restrict__ pairs,
             int pair_count, int luma_height, int luma_width, int colour_height, int colour_width, float level,
             double* __restrict__ out)
{
    const int plane = blockIdx.x / pair_count;
    const int pair = blockIdx.x - plane * pair_count;
    const int height = plane == 0 ? luma_height : colour_height;
    const int width = plane == 0 ? luma_width : colour_width;
    const long long luma_size = (long long)luma_height * luma_width;
    const long long colour_size = (long long)colour_height * colour_width;
    const long long offset = plane == 0 ? 0 : luma_size + (plane - 1) * colour_size;
    const float* a = left + (long long)pairs[2 * pair] * (luma_size + 2 * colour_size) + offset;
    const float* b = right + (long long)pairs[2 * pair + 1] * (luma_size + 2 * colour_size) + offset;

    __shared__ int next_item;
    __shared__ double partial[NUM_METRICS][32];
    if (threadIdx.x == 0) next_item = 0;

    double best[NUM_METRICS];
#pragma unroll
    for (int m = 0; m < NUM_METRICS; ++m) best[m] = 0.0;

    // peak: the largest |a - b| (float32 value widened), over coalesced strides.
    float peak = 0.0f;
    const int count = height * width;
    for (int i = threadIdx.x; i < count; i += blockDim.x)
        peak = fmaxf(peak, fabsf(__fsub_rn(__ldg(a + i), __ldg(b + i))));
    best[0] = (double)peak;
    __syncthreads();

    // The items, heaviest first: signed 32, 16, 8 windows (regular starts every SIZE / 2 and one at the far edge),
    // then the local8 / concentration8 tiles of both column phases, then the local4 ones.
    const int windows32 = (width - 32) / 16 + 2;
    const int windows16 = (width - 16) / 8 + 2;
    const int windows8 = (width - 8) / 4 + 2;
    const int first_tiles8 = (width + 7) / 8;
    const int tiles8 = first_tiles8 + 1 + (width - 4 + 7) / 8;
    const int first_tiles4 = (width + 3) / 4;
    const int tiles4 = first_tiles4 + 1 + (width - 2 + 3) / 4;
    const int total = windows32 + windows16 + windows8 + tiles8 + tiles4;

    for (;;) {
        int item = atomicAdd(&next_item, 1);
        if (item >= total) break;
        if (item < windows32) {
            const int x = item * 16 < width - 32 ? item * 16 : width - 32;
            best[6] = fmax(best[6], signed_column<32>(a, b, height, width, x));
            continue;
        }
        item -= windows32;
        if (item < windows16) {
            const int x = item * 8 < width - 16 ? item * 8 : width - 16;
            best[5] = fmax(best[5], signed_column<16>(a, b, height, width, x));
            continue;
        }
        item -= windows16;
        if (item < windows8) {
            const int x = item * 4 < width - 8 ? item * 4 : width - 8;
            best[4] = fmax(best[4], signed_column<8>(a, b, height, width, x));
            continue;
        }
        item -= windows8;
        int x_begin, x_end;
        if (item < tiles8) {
            tile_span(width, 8, first_tiles8, item, &x_begin, &x_end);
            tile_column<8, true>(a, b, height, width, x_begin, x_end, level, &best[2], &best[3]);
            continue;
        }
        item -= tiles8;
        tile_span(width, 4, first_tiles4, item, &x_begin, &x_end);
        tile_column<4, false>(a, b, height, width, x_begin, x_end, level, &best[1], &best[1]);
    }

    // The block's maxima: per warp by shuffles, then across the warps. The signed ones are divided by the window area
    // once, after the maximum (the CPU divides the maximum).
    const int lane = threadIdx.x & 31;
    const int warp = threadIdx.x >> 5;
#pragma unroll
    for (int m = 0; m < NUM_METRICS; ++m) {
        const double top = warp_max(best[m]);
        if (lane == 0) partial[m][warp] = top;
    }
    __syncthreads();
    const int warps = (blockDim.x + 31) >> 5;
    for (int m = threadIdx.x; m < NUM_METRICS; m += blockDim.x) {
        double top = partial[m][0];
        for (int w = 1; w < warps; ++w) top = fmax(top, partial[m][w]);
        if (m >= 4) {
            const int side = 8 << (m - 4);
            top = __ddiv_rn(top, (double)(side * side));
        }
        out[((long long)pair * 3 + plane) * NUM_METRICS + m] = top;
    }
}
