"""Read, modify and write FMOD Studio 2.02 .bank files, as used by Trivia Murder Party 2's TMP2Host.bank.

Format notes (reverse engineered from TMP2Host.bank):
- The bank is a RIFF 'FEV ' file. Plain chunks are padded to an even size.
- Arrays: u16 header h; count = h >> 1. If h & 1 the elements have a fixed size, given by a u16 that
  follows when count > 0. Otherwise each element is prefixed by its own u16 size.
- TLNB (timeline body): GUID then 5 arrays: [0] other instruments (e.g. programmer sounds),
  [1] waveform instruments (fixed 24 bytes: GUID, u32 start, u32 length), [2] unused here,
  [3] markers/regions (GUID, u32 position, u16 name length, name, u32 length), [4] unused here.
  Timeline positions and lengths are in 48 kHz samples.
- WAIB: instrument GUID, waveform GUID. WAV: waveform GUID, u32, u16 sound table index,
  u16 subsound index, u16, u16 loading mode (2 = stream), u16.
- SNDH: array of (u32 absolute file offset, u32 size), one per FSB5 inside the SND chunk.
- FSB5 v1: 60 byte header, then per sample a u64 (bit 0 more chunks, bits 1-4 rate index,
  bits 5-6 channels - 1, bits 7-33 data offset / 32, bits 34-63 sample count) plus optional chunks,
  then the name table, then the sample data (each sample 32 byte aligned).
"""
import array, struct, uuid

FREQS = {1: 8000, 2: 11000, 3: 11025, 4: 16000, 5: 22050, 6: 24000, 7: 32000, 8: 44100, 9: 48000}
FREQ_INDEX = {v: k for k, v in FREQS.items()}
FSB_PCM16, FSB_VORBIS = 2, 15
CHUNK_PEAK_VOLUME = 13
TIMELINE_RATE = 48000


class Chunk:
    def __init__(self, cid, data=b"", ltype=None, children=None):
        self.cid, self.data, self.ltype, self.children = cid, data, ltype, children

    def __iter__(self):
        yield self
        for c in self.children or ():
            yield from c

    def find(self, cid, ltype=None):
        return [c for c in self if c.cid == cid and (ltype is None or c.ltype == ltype)]


def parse_chunks(buf, off, end):
    out = []
    while off + 8 <= end:
        cid = buf[off:off + 4].decode("latin1")
        size, = struct.unpack_from("<I", buf, off + 4)
        if cid in ("RIFF", "LIST"):
            ltype = buf[off + 8:off + 12].decode("latin1")
            out.append(Chunk(cid, ltype=ltype, children=parse_chunks(buf, off + 12, off + 8 + size)))
        else:
            out.append(Chunk(cid, data=buf[off + 8:off + 8 + size]))
        off += 8 + size + (size & 1)
    return out


def serialize(chunk, base=0, positions=None):
    """Chunk -> bytes. Records the absolute data offset of every plain chunk in `positions`."""
    if chunk.children is not None:
        body = bytearray(chunk.ltype.encode("latin1"))
        for c in chunk.children:
            body += serialize(c, base + 8 + len(body), positions)
    else:
        body = chunk.data
        if positions is not None:
            positions[id(chunk)] = base + 8
    out = bytearray(chunk.cid.encode("latin1")) + struct.pack("<I", len(body)) + body
    if len(body) & 1:
        out += b"\0"
    return bytes(out)


def read_array(b, p):
    h, = struct.unpack_from("<H", b, p)
    p += 2
    count, fixed = h >> 1, bool(h & 1)
    items = []
    if fixed and count:
        size, = struct.unpack_from("<H", b, p)
        p += 2
        for _ in range(count):
            items.append(b[p:p + size])
            p += size
    elif not fixed:
        for _ in range(count):
            size, = struct.unpack_from("<H", b, p)
            items.append(b[p + 2:p + 2 + size])
            p += 2 + size
    return items, fixed, p


def write_array(items, fixed):
    out = bytearray(struct.pack("<H", len(items) << 1 | int(fixed)))
    if fixed and items:
        out += struct.pack("<H", len(items[0]))
        for it in items:
            assert len(it) == len(items[0])
            out += it
    elif not fixed:
        for it in items:
            out += struct.pack("<H", len(it)) + it
    return bytes(out)


class Timeline:
    def __init__(self, data):
        self.guid = data[:16]
        p = 16
        self.arrays = []
        for _ in range(5):
            items, fixed, p = read_array(data, p)
            self.arrays.append([items, fixed])
        assert p == len(data), "unexpected timeline layout"
        self.markers = []
        for m in self.arrays[3][0]:
            pos, nlen = struct.unpack_from("<IH", m, 16)
            length, = struct.unpack_from("<I", m, 22 + nlen)
            assert 26 + nlen == len(m), "unexpected marker layout"
            self.markers.append(dict(guid=m[:16], pos=pos, name=m[22:22 + nlen].decode("utf-8"), length=length))

    @property
    def instruments(self):
        return self.arrays[1][0]

    def set_instrument_length(self, i, length):
        self.instruments[i] = self.instruments[i][:20] + struct.pack("<I", length)

    def set_markers(self, markers):
        """markers: [(position in 48 kHz samples, name)]. Reuses the existing marker GUIDs first."""
        old = self.markers
        self.markers = [dict(guid=old[k]["guid"] if k < len(old) else uuid.uuid4().bytes_le,
                             pos=int(pos), name=name, length=0) for k, (pos, name) in enumerate(markers)]

    def build(self):
        recs = []
        for m in self.markers:
            n = m["name"].encode("utf-8")
            recs.append(m["guid"] + struct.pack("<IH", m["pos"], len(n)) + n + struct.pack("<I", m["length"]))
        self.arrays[3][0] = recs
        return self.guid + b"".join(write_array(items, fixed) for items, fixed in self.arrays)


class Sample:
    def __init__(self, name, rate, channels, num_samples, data, chunks):
        self.name, self.rate, self.channels, self.num_samples = name, rate, channels, num_samples
        self.data, self.chunks = data, chunks  # chunks: [(type, bytes)]


def pcm16_sample(name, pcm, rate=48000):
    """Mono PCM16 sample with a peak volume chunk, like the ones FMOD Studio writes."""
    a = array.array("h", pcm)
    peak = max(max(a, default=0), -min(a, default=0)) / 32768
    return Sample(name, rate, 1, len(a), pcm, [(CHUNK_PEAK_VOLUME, struct.pack("<f", max(peak, 1e-4)))])


class FSB5:
    def __init__(self, mode, extra, samples):
        self.mode, self.extra, self.samples = mode, extra, samples

    @classmethod
    def parse(cls, b):
        assert b[:4] == b"FSB5"
        ver, num, shs, nts, ds, mode = struct.unpack_from("<6I", b, 4)
        assert ver == 1, "only FSB5 version 1 is supported"
        p, heads = 60, []
        for _ in range(num):
            raw, = struct.unpack_from("<Q", b, p)
            p += 8
            more, chunks = raw & 1, []
            while more:
                c, = struct.unpack_from("<I", b, p)
                p += 4
                more, size, typ = c & 1, (c >> 1) & 0xFFFFFF, (c >> 25) & 0x7F
                chunks.append((typ, b[p:p + size]))
                p += size
            heads.append((FREQS[(raw >> 1) & 0xF], ((raw >> 5) & 3) + 1, ((raw >> 7) & 0x7FFFFFF) << 5,
                          (raw >> 34) & 0x3FFFFFFF, chunks))
        nt = 60 + shs
        names = [None] * num
        if nts:
            offs = struct.unpack_from(f"<{num}I", b, nt)
            names = [b[nt + o:b.index(b"\0", nt + o)].decode() for o in offs]
        data0 = 60 + shs + nts
        samples = []
        for k, (rate, ch, off, ns, chunks) in enumerate(heads):
            end = heads[k + 1][2] if k + 1 < num else ds
            samples.append(Sample(names[k], rate, ch, ns, b[data0 + off:data0 + end], chunks))
        return cls(mode, b[28:60], samples)

    def build(self):
        heads, data = bytearray(), bytearray()
        for s in self.samples:
            off = len(data)
            raw = (int(bool(s.chunks)) | FREQ_INDEX[s.rate] << 1 | (s.channels - 1) << 5
                   | (off >> 5) << 7 | s.num_samples << 34)
            heads += struct.pack("<Q", raw)
            for k, (typ, cd) in enumerate(s.chunks):
                heads += struct.pack("<I", int(k + 1 < len(s.chunks)) | len(cd) << 1 | typ << 25) + cd
            data += s.data
            data += b"\0" * (-len(data) % 32)
        names = b""
        if any(s.name for s in self.samples):
            tbl, offs = bytearray(), []
            for s in self.samples:
                offs.append(4 * len(self.samples) + len(tbl))
                tbl += s.name.encode() + b"\0"
            names = struct.pack(f"<{len(offs)}I", *offs) + tbl
            names += b"\0" * (-(60 + len(heads) + len(names)) % 32)  # sample data starts 32 aligned
        hdr = b"FSB5" + struct.pack("<6I", 1, len(self.samples), len(heads), len(names), len(data), self.mode)
        return hdr + self.extra + bytes(heads) + names + bytes(data)


class Bank:
    def __init__(self, path):
        buf = open(path, "rb").read()
        self.root = parse_chunks(buf, 0, len(buf))[0]
        self._snd = self.root.find("SND ")[0]
        self._sndh = self.root.find("SNDH")[0]
        entries, _, _ = read_array(self._sndh.data, 0)
        self.fsbs = []
        for e in entries:
            off, size = struct.unpack("<II", e)
            self.fsbs.append(FSB5.parse(buf[off:off + size]))

    def fsb_bytes(self, i=0):
        return self.fsbs[i].build()

    def timeline_for_sample(self, sample_name, table=0):
        """(timeline chunk, Timeline, instrument index) of the waveform instrument playing a sample."""
        sub = [s.name for s in self.fsbs[table].samples].index(sample_name)
        wave = next(c.data[:16] for c in self.root.find("WAV ")
                    if struct.unpack_from("<HH", c.data, 20) == (table, sub))
        inst_guid = next(c.data[:16] for c in self.root.find("WAIB") if c.data[16:32] == wave)
        for c in self.root.find("TLNB"):
            t = Timeline(c.data)
            for k, ins in enumerate(t.instruments):
                if ins[:16] == inst_guid:
                    return c, t, k
        raise KeyError(sample_name)

    def save(self, path, align=32):
        blobs = [f.build() for f in self.fsbs]
        # pass 1 with placeholder SNDH to learn where the SND payload lands
        self._sndh.data = write_array([b"\0" * 8] * len(blobs), True)
        self._snd.data = b""
        positions = {}
        serialize(self.root, 0, positions)
        start = positions[id(self._snd)]
        payload, table = bytearray(), []
        for blob in blobs:
            payload += b"\0" * (-(start + len(payload)) % align)
            table.append(struct.pack("<II", start + len(payload), len(blob)))
            payload += blob
        self._snd.data = bytes(payload)
        self._sndh.data = write_array(table, True)
        out = serialize(self.root)
        open(path, "wb").write(out)
        return len(out)
