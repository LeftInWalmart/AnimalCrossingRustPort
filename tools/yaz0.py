"""Yaz0 decompressor (Nintendo SZS)."""
import struct, sys

def decompress(src: bytes) -> bytes:
    assert src[:4] == b"Yaz0", "not Yaz0"
    size = struct.unpack_from(">I", src, 4)[0]
    dst = bytearray(size)
    s, d = 16, 0
    while d < size:
        code = src[s]; s += 1
        for bit in range(8):
            if d >= size:
                break
            if code & (0x80 >> bit):
                dst[d] = src[s]; d += 1; s += 1
            else:
                b1, b2 = src[s], src[s + 1]; s += 2
                dist = ((b1 & 0x0F) << 8 | b2) + 1
                n = b1 >> 4
                if n == 0:
                    n = src[s] + 0x12; s += 1
                else:
                    n += 2
                for _ in range(n):
                    dst[d] = dst[d - dist]; d += 1
    return bytes(dst)

if __name__ == "__main__":
    open(sys.argv[2], "wb").write(decompress(open(sys.argv[1], "rb").read()))
