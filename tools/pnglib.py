"""最小 PNG 讀寫（RGBA / RGB / 索引色），純 stdlib。"""
import struct
import zlib


def read_png(path):
    """回傳 2D list[y][x] = (r,g,b,a)"""
    data = open(path, 'rb').read()
    pos = 8
    idat = b''
    plte = trns = None
    ctype = 6
    W = H = 0
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]
        t = data[pos + 4:pos + 8]
        d = data[pos + 8:pos + 8 + ln]
        if t == b'IHDR':
            W, H, _, ctype = struct.unpack('>IIBB', d[:10])
        elif t == b'PLTE':
            plte = d
        elif t == b'tRNS':
            trns = d
        elif t == b'IDAT':
            idat += d
        pos += 12 + ln
    raw = zlib.decompress(idat)
    ch = {6: 4, 2: 3, 3: 1, 0: 1}[ctype]
    stride = W * ch + 1
    prev = [0] * (W * ch)
    px = []
    for y in range(H):
        f = raw[y * stride]
        line = list(raw[y * stride + 1:(y + 1) * stride])
        for i in range(len(line)):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p_ = a + b - c
                pa, pb, pc = abs(p_ - a), abs(p_ - b), abs(p_ - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        prev = line
        if ctype == 6:
            px.append([tuple(line[x * 4:x * 4 + 4]) for x in range(W)])
        elif ctype == 2:
            px.append([tuple(line[x * 3:x * 3 + 3]) + (255,) for x in range(W)])
        else:
            assert plte is not None, "索引色 PNG 缺 PLTE"
            row = []
            for x in range(W):
                idx = line[x]
                r, g, b = plte[idx * 3:idx * 3 + 3]
                a = trns[idx] if trns and idx < len(trns) else 255
                row.append((r, g, b, a))
            px.append(row)
    return px


def write_png(path, px):
    H, W = len(px), len(px[0])
    raw = b''.join(b'\x00' + b''.join(struct.pack('4B', *p) for p in row) for row in px)
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    open(path, 'wb').write(
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 6, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def crop(px, x, y, w, h):
    return [[px[y + yy][x + xx] for xx in range(w)] for yy in range(h)]
