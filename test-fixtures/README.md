# JPEG regression inputs

`valid-64.jpg` is the original 64 × 64 baseline fixture. The strict decoder test
keeps half of its 90 entropy bytes and appends a valid EOI marker: macOS `sips`
accepts that damaged file and fills the missing image area. The entropy checker
must reject it before invoking a pixel decoder.

The four `imageio-*.jpg` files were independently encoded with macOS ImageIO on
2026-11-27 by `generate_jpeg.c`, whose deterministic pixels are generated from
coordinates. They contain no user photos or private metadata. Color images are
37 × 29; gray images are 17 × 19. Both baseline (SOF0) and progressive (SOF2)
variants are checked. Compile the generator as documented at its start if the
fixtures need to be regenerated; normal tests use the checked-in files.

`test_image_decode.py` additionally builds small JPEG byte streams from explicit
Huffman symbols to exercise 4:4:4, 4:2:2 and 4:2:0 sampling, partial edge blocks,
restart intervals and sequence rollover, progressive EOB runs, zero runs, and
refinement of existing and newly nonzero AC coefficients. These valid streams
also pass the native pixel decoder. The malformed streams test missing entropy,
bad restart boundaries, excessive EOB runs, invalid padding and unused bytes.

No ZIP is required to create or use any fixture.
