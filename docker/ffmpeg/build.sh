#!/bin/bash
set -euo pipefail

readonly VERSION=8.1.2
readonly SHA256=464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c
readonly ROOT=/tmp/ffmpeg-gpl-build
readonly PREFIX=/opt/ffmpeg-gpl
readonly JOBS="${FFMPEG_JOBS:-4}"
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'FFMPEG_JOBS must be a positive integer' >&2; exit 1; }

mkdir -p "$ROOT/x264" "$PREFIX/share/source/x264" "$PREFIX/share/source/ffmpeg"
cp /tmp/ffmpeg-gpl-Dockerfile "$PREFIX/share/source/Dockerfile"
dpkg-query -W -f='${binary:Package}\t${Version}\n' | LC_ALL=C sort > "$PREFIX/share/source/build-packages.tsv"
mkdir -p "$PREFIX/share/source/apt"
if [[ -f /etc/apt/sources.list ]]; then
    cp /etc/apt/sources.list "$PREFIX/share/source/apt/"
fi
cp -a /etc/apt/sources.list.d "$PREFIX/share/source/apt/"
apt-cache policy > "$PREFIX/share/source/apt/policy.txt"
cd "$ROOT/x264"
# APT authenticates the Ubuntu source index and the source package checksums.
# Retain the exact downloaded source package, including Ubuntu's patches.
apt-get source x264
cp ./*.dsc ./*.tar.* "$PREFIX/share/source/x264/"
for package in chromaprint zimg nv-codec-headers; do
    mkdir -p "$PREFIX/share/source/$package" "$ROOT/$package"
    cd "$ROOT/$package"
    case "$package" in
        chromaprint) binary=libchromaprint1 ;;
        zimg) binary=libzimg2 ;;
        # Header-only NVIDIA codec API (MIT): FFmpeg loads libcuda/libnvcuvid at run time from the NVIDIA runtime.
        nv-codec-headers) binary=libffmpeg-nvenc-dev ;;
    esac
    version="$(dpkg-query -W -f='${source:Version}' "$binary")"
    apt-get source "$package=$version"
    cp ./*.dsc ./*.tar.* "$PREFIX/share/source/$package/"
    cp "/usr/share/doc/$binary/copyright" "$PREFIX/share/source/$package/copyright"
done
cd "$ROOT/x264"
x264_source=(x264-*/)
cd "${x264_source[0]}"
x264_args=(--prefix="$PREFIX" --enable-static --disable-cli --disable-opencl --enable-pic)
printf '%s\n' "${x264_args[@]}" > "$PREFIX/share/source/x264/configure-args.txt"
cp COPYING "$PREFIX/share/source/x264/"
./configure "${x264_args[@]}"
make -j"$JOBS"
make install

cd "$ROOT"
archive="ffmpeg-${VERSION}.tar.xz"
curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --retry-connrefused \
    "https://ffmpeg.org/releases/$archive" -o "$archive"
printf '%s  %s\n' "$SHA256" "$archive" | sha256sum --check --status
cp "$archive" "$PREFIX/share/source/ffmpeg/"
cp /tmp/ffmpeg-gpl-build.sh "$PREFIX/share/source/build.sh"
tar -xf "$archive"
cd "ffmpeg-$VERSION"
cp COPYING.GPLv2 COPYING.GPLv3 COPYING.LGPLv2.1 COPYING.LGPLv3 LICENSE.md "$PREFIX/share/source/ffmpeg/"
printf '%s\n' 'This software is based in part on the work of the Independent JPEG Group.' \
    > "$PREFIX/share/source/ffmpeg/NOTICE-IJG.txt"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
ffmpeg_args=(
    --prefix="$PREFIX"
    --disable-autodetect
    --enable-gpl
    --disable-nonfree
    --enable-libx264
    --enable-libzimg
    --enable-chromaprint
    --enable-gnutls
    --enable-zlib
    --enable-bzlib
    --enable-lzma
    # Hardware decode, redistributable: NVDEC/CUVID through the MIT nv-codec-headers (libcuda and libnvcuvid are
    # loaded at run time from the NVIDIA container runtime) and VA-API through libva. cuda-llvm compiles the CUDA
    # filters (scale_cuda) with clang, so no nvcc/NPP and no --enable-nonfree. Encoders are deliberately left out.
    --enable-ffnvcodec
    --enable-cuda
    --enable-cuda-llvm
    --enable-cuvid
    --enable-nvdec
    --enable-vaapi
    # Vulkan (--enable-vulkan, libplacebo) is left out on purpose: it only serves Dolby Vision profile 5 preview
    # thumbnails, which Reprise doesn't make. The app reads configure-args.txt (written below) to tell a build that
    # leaves it out from a broken one (gpu/ffmpeg_capabilities.py ffmpeg_vulkan_left_out); a build with Vulkan in
    # these arguments is expected to have it working.
    --disable-shared
    --enable-static
    --disable-debug
    --disable-doc
    --extra-cflags="-I$PREFIX/include"
    --extra-ldflags="-L$PREFIX/lib"
)
printf '%s\n' "${ffmpeg_args[@]}" > "$PREFIX/share/source/ffmpeg/configure-args.txt"
./configure "${ffmpeg_args[@]}"
make -j"$JOBS"
make install

# Keep the resolved shared-library closure alongside the exact binaries. The
# ELF interpreter is deliberately left to the host; wrappers can point only
# these programs at this directory with LD_LIBRARY_PATH.
mkdir -p "$PREFIX/runtime-libs"
notices="$PREFIX/share/source/runtime-library-notices"
mkdir -p "$notices"
: > "$notices/owners.tsv"
for executable in "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe"; do
    dependencies="$(ldd "$executable")"
    while read -r soname arrow path rest; do
        [[ "$arrow" == '=>' ]] || continue
        if [[ "$path" != /* || ! -f "$path" ]]; then
            echo "Missing dependency for $executable: $soname $arrow $path" >&2
            exit 1
        fi
        resolved="$(readlink -f "$path")"
        owner_records=""
        for candidate in "$path" "$resolved"; do
            case "$candidate" in
                /usr/lib*) alias="${candidate#/usr}" ;;
                /lib*) alias="/usr$candidate" ;;
                *) alias="$candidate" ;;
            esac
            for variant in "$candidate" "$alias"; do
                if found="$(dpkg-query -S "$variant" 2>/dev/null)"; then
                    owner_records+="$found"$'\n'
                fi
            done
        done
        if [[ -z "$owner_records" ]]; then
            echo "No installed package owns shared library: $path" >&2
            exit 1
        fi
        while IFS= read -r record; do
            [[ -n "$record" ]] || continue
            if [[ "$record" != *': /'* ]]; then
                echo "Unexpected package ownership record: $record" >&2
                exit 1
            fi
            owners="${record%%: /*}"
            IFS=',' read -ra owner_list <<< "$owners"
            for owner in "${owner_list[@]}"; do
                owner="${owner//[[:space:]]/}"
                package="${owner%%:*}"
                copyright="/usr/share/doc/$package/copyright"
                if [[ ! -f "$copyright" ]]; then
                    echo "Missing copyright notice for $owner ($path)" >&2
                    exit 1
                fi
                info="$(dpkg-query -W -f='${db:Status-Status}\t${binary:Package}\t${Version}\t${source:Package}\t${source:Version}' "$owner")"
                if [[ "$info" != $'installed\t'* ]]; then
                    echo "Library owner is not installed: $owner ($path)" >&2
                    exit 1
                fi
                cp -L "$copyright" "$notices/$package.copyright"
                printf '%s\t%s\t%s\n' "$soname" "$path" "$info" >> "$notices/owners.tsv"
            done
        done <<< "$owner_records"
        if [[ -e "$PREFIX/runtime-libs/$soname" ]]; then
            cmp -s "$path" "$PREFIX/runtime-libs/$soname" || {
                echo "Conflicting dependency: $soname" >&2
                exit 1
            }
        else
            cp -L "$path" "$PREFIX/runtime-libs/$soname"
        fi
    done <<< "$dependencies"
done
LC_ALL=C sort -u -o "$notices/owners.tsv" "$notices/owners.tsv"
