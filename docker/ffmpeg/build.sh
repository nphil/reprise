#!/bin/bash
set -euo pipefail

readonly VERSION=8.1.2
readonly SHA256=464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c
readonly ROOT=/tmp/ffmpeg-gpl-build
readonly PREFIX=/opt/ffmpeg-gpl
readonly JOBS="${FFMPEG_JOBS:-4}"
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'FFMPEG_JOBS must be a positive integer' >&2; exit 1; }

mkdir -p "$ROOT/x264" "$PREFIX/share/source/x264" "$PREFIX/share/source/ffmpeg"
cd "$ROOT/x264"
# APT authenticates the Ubuntu source index and the source package checksums.
# Retain the exact downloaded source package, including Ubuntu's patches.
apt-get source x264
cp ./*.dsc ./*.tar.* "$PREFIX/share/source/x264/"
for package in chromaprint zimg; do
    mkdir -p "$PREFIX/share/source/$package" "$ROOT/$package"
    cd "$ROOT/$package"
    case "$package" in
        chromaprint) binary=libchromaprint1 ;;
        zimg) binary=libzimg2 ;;
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
for executable in "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe"; do
    dependencies="$(ldd "$executable")"
    while read -r soname arrow path rest; do
        [[ "$arrow" == '=>' ]] || continue
        if [[ "$path" != /* || ! -f "$path" ]]; then
            echo "Missing dependency for $executable: $soname $arrow $path" >&2
            exit 1
        fi
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
