"""
DMR (ETSI TS 102 361) Tier II / III burst decoder -- Embedded Python Block, GNU Radio 3.10

Input  : float stream at `sps` samples per symbol (10 = 48 kS/s), the RRC-filtered
         discriminator output; nominal levels +-1 / +-3 (Quadrature Demod gain =
         fs/(2*pi*648)).
Output : stream 0: decoded voice, 8 kHz float (+-1), silence when no call.
         stream 1: symbol samples (sample-and-hold, 8 kHz) for a histogram display.
         message port 'out' -> one u8vector PDU per printed line. Lines are printed too.

Timing is FEED-FORWARD: every 48-bit sync is located to a fraction of a sample by
correlation, and that burst is sampled at exactly that timing (bursts without sync,
voice B-F and the CACH grid, reuse the timing of the nearest sync). There is no
feedback timing loop, so nothing can drift or wind up while the signal is absent
or being retuned: the first sync after a retune is decoded correctly. Gain and DC
(carrier offset) are likewise re-estimated from every sync.

Decodes: BS (repeater), MS and direct-mode syncs; CACH (TACT -> time slot, Short LC:
system identity / activity); slot type (Golay 20,8); BPTC(196,96) bursts: CSBK,
voice LC header, terminator, PI header, data header; voice superframes with EMB
(QR 16,7) and embedded LC (late entry). Voice audio (AMBE+2) is NOT decoded.

Why an EPB: none of the above exists as a stock GNU Radio block.

Code tables were extracted from MMDVMHost (G4KLX) encoder classes; the decoder was
validated against burst streams generated with those classes and against a captured
voice LC header from dmr_utils3.

Voice audio: AMBE+2 frames are handed to mbelib-neo (mbe-neo.dll, soft-decision API)
or classic mbelib, loaded at run time with ctypes (parameter mbelib_path; empty =
look next to the flowgraph and on PATH). Without the library everything else still
works and the audio output stays silent. Encrypted calls are muted.
"""
import sys
import time
import threading
import numpy as np
import pmt
from gnuradio import gr

DIBIT_LEVEL = np.array([1.0, 3.0, -1.0, -3.0])        # dibit 00,01,10,11 -> +1,+3,-1,-3

# voice syncs; the matching data sync is the sign-inverted pattern
SYNCS = {'BS': 0x755FD7DF75F7, 'MS': 0x7F7D5DD57DFD, 'DM1': 0x5D577F7757FF, 'DM2': 0x7DFFD5F55D5F}
P25_FS = 0x5575F5FF77FF

GOLAY2087_ROWS = (0x803DA, 0x40D99, 0x206CD, 0x10367, 0x08DC6, 0x04A97, 0x0293E, 0x018EB)
QR1676_ROWS = (0x804F, 0x411E, 0x21B7, 0x11E2, 0x09C9, 0x04E5, 0x0273)
HAM15113_ROWS = (0x4009, 0x200D, 0x100F, 0x080E, 0x0407, 0x020A, 0x0105, 0x008B, 0x004C, 0x0026, 0x0013)
HAM1393_ROWS = (0x100F, 0x080E, 0x0407, 0x020A, 0x0105, 0x008B, 0x004C, 0x0026, 0x0013)
HAM16114_ROWS = (0x8013, 0x401A, 0x201F, 0x101C, 0x080E, 0x0415, 0x020B, 0x0116, 0x0099, 0x004D, 0x0027)
HAM17123_ROWS = (0x1001B, 0x0801F, 0x0401D, 0x0201C, 0x0100E, 0x00807, 0x00411, 0x0021A, 0x0010D,
                 0x00094, 0x0004A, 0x00025)
TACT_ROWS = (0b1000101, 0b0100111, 0b0010110, 0b0001011)     # AT TC LS1 LS0 | H0 H1 H2
CACH_TACT_POS = (0, 4, 8, 12, 14, 18, 22)
CACH_PAYLOAD_POS = (1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 15, 16, 17, 19, 20, 21, 23)

DT_NAME = {0: 'PI HDR', 1: 'VOICE HDR', 2: 'TERMINATOR', 3: 'CSBK', 4: 'MBC HDR', 5: 'MBC CONT',
           6: 'DATA HDR', 7: 'DATA 1/2', 8: 'DATA 3/4', 9: 'IDLE', 10: 'DATA 1/1'}
CRC_MASK = {0: 0x6969, 3: 0xA5A5, 4: 0xAAAA, 6: 0xCCCC}
RS_MASK = {1: 0x969696, 2: 0x999999}
FID_NAME = {0x00: 'std', 0x04: 'Flyde', 0x06: 'Trident', 0x08: 'Hytera', 0x10: 'Motorola',
            0x58: 'Tait', 0x68: 'Hytera'}
CSBKO_NAME = {0x04: 'UU_V_Req', 0x05: 'UU_Ans_Rsp', 0x07: 'CT_CSBK', 0x19: 'C_ALOHA', 0x1A: 'C_UDT_HEAD',
              0x1C: 'C_AHOY', 0x1E: 'C_ACKVIT', 0x1F: 'C_RAND', 0x20: 'C_ACKD', 0x21: 'C_ACKU',
              0x22: 'P_ACKD', 0x23: 'P_ACKU', 0x24: 'RADIO_CHECK', 0x26: 'NACK_RSP', 0x27: 'CALL_EMERGENCY',
              0x28: 'C_BCAST', 0x29: 'C_MAINT', 0x2A: 'P_MAINT', 0x2E: 'P_CLEAR', 0x2F: 'P_PROTECT',
              0x30: 'PV_GRANT', 0x31: 'TV_GRANT', 0x32: 'BTV_GRANT', 0x33: 'PD_GRANT', 0x34: 'TD_GRANT',
              0x35: 'PV_GRANT_DX', 0x36: 'PD_GRANT_DX', 0x37: 'PD_GRANT_MI', 0x38: 'BS_DWN_ACT', 0x39: 'C_MOVE',
              0x3D: 'PREAMBLE'}
# ETSI reserved "gateway" addresses (TS 102 361-4), names as used by DSD-FME
GW_NAME = {0xFFFEC0: 'PSTNI', 0xFFFEC1: 'PABXI', 0xFFFEC2: 'LINEI', 0xFFFEC3: 'IPI', 0xFFFEC4: 'SUPLI',
           0xFFFEC5: 'SDMI(short-data gw)', 0xFFFEC6: 'REGI(registration gw)', 0xFFFEC7: 'MSI',
           0xFFFEC9: 'DIVERTI', 0xFFFECA: 'TSI', 0xFFFECB: 'DISPATI', 0xFFFECC: 'STUNI', 0xFFFECD: 'AUTHI',
           0xFFFECE: 'GPI', 0xFFFECF: 'KILLI', 0xFFFED0: 'PSTNDI', 0xFFFED1: 'PABXDI', 0xFFFED2: 'LINEDI',
           0xFFFED3: 'DISPATDI', 0xFFFED4: 'ALLMSI', 0xFFFED5: 'IPDI', 0xFFFED6: 'DGNAI', 0xFFFED7: 'TATTSI',
           0xFFFFFD: 'ALLMSIDL', 0xFFFFFE: 'ALLMSIDZ', 0xFFFFFF: 'ALLMSID'}
AHOY_KIND = {0: 'voice call', 1: 'voice call', 2: 'packet data', 3: 'packet data', 4: 'short data (UDT)',
             5: 'short data (UDT)', 6: 'short-data polling', 7: 'status', 8: 'call diversion', 9: 'call answer',
             10: 'duplex voice', 11: 'duplex data', 13: 'stun/revive/kill/auth', 14: 'registration/authentication',
             15: 'cancel call'}


def addr(a):
    """radio ID, or the name of a reserved system address"""
    if a in GW_NAME:
        return GW_NAME[a]
    if a >= 0xFFFEC0:
        return 'sys-%06X' % a
    return str(a)
DPF_NAME = {0: 'UDT', 1: 'response', 2: 'unconfirmed', 3: 'confirmed', 13: 'short defined',
            14: 'short raw', 15: 'proprietary'}
ACT_NAME = {0: 'idle', 2: 'grp CSBK', 3: 'ind CSBK', 8: 'grp voice', 9: 'ind voice', 10: 'ind data',
            11: 'grp data', 12: 'grp EMERG', 13: 'ind EMERG'}


def _fs_syms(fs):
    return np.array([DIBIT_LEVEL[(fs >> (46 - 2 * i)) & 3] for i in range(24)])


def level_to_dibit(z):
    d = np.empty(len(z), dtype=np.uint8)
    d[z >= 2.0] = 1
    d[(z >= 0.0) & (z < 2.0)] = 0
    d[(z < 0.0) & (z >= -2.0)] = 2
    d[z < -2.0] = 3
    return d


def dibits_to_bits(d):
    out = np.empty(2 * len(d), dtype=np.uint8)
    out[0::2] = (d >> 1) & 1
    out[1::2] = d & 1
    return out


def syms_to_llr(z):
    """soft bits (positive -> 1) from normalised 4-level symbols"""
    out = np.empty(2 * len(z))
    out[0::2] = -z
    out[1::2] = np.abs(z) - 2.0
    return out


def bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def int_bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


class SoftCode(object):
    """ML decoder of a short systematic block code (soft bits)."""

    def __init__(self, rows, k, n):
        self.k, self.n = k, n
        cw = np.zeros(1 << k, dtype=np.int64)
        data = np.arange(1 << k)
        for i, r in enumerate(rows):
            cw ^= ((data >> (k - 1 - i)) & 1) * r
        self.cw = cw
        sh = np.arange(n - 1, -1, -1)
        self.B = ((cw[:, None] >> sh[None, :]) & 1).astype(np.float32)
        self.S = 2.0 * self.B - 1.0

    def decode(self, llr):
        """returns (data, hard bit errors vs. chosen codeword)"""
        i = int(np.argmax(self.B @ np.asarray(llr, np.float32)))
        hard = (np.asarray(llr) > 0).astype(np.int64)
        return i, int(np.sum(hard != self.B[i]))


class HardHamming(object):
    """single-error correcting systematic code from generator rows (data first)"""

    def __init__(self, rows, k, n):
        self.k, self.n, self.rows = k, n, rows
        self.np_ = n - k
        self.par = [r & ((1 << self.np_) - 1) for r in rows]
        self.syn = {}
        for pos in range(n):                       # bit position 0 = first (MSB)
            if pos < k:
                s = self.par[pos]
            else:
                s = 1 << (n - 1 - pos)
            self.syn[s] = pos

    def parity(self, bits):
        p = 0
        for i in range(self.k):
            if bits[i]:
                p ^= self.par[i]
        return p

    def correct(self, bits):
        """bits: list (modified in place). returns 0 ok, 1 corrected, -1 fail"""
        rp = 0
        for i in range(self.k, self.n):
            rp = (rp << 1) | bits[i]
        s = self.parity(bits) ^ rp
        if s == 0:
            return 0
        pos = self.syn.get(s)
        if pos is None:
            return -1
        bits[pos] ^= 1
        return 1


HAM15 = HardHamming(HAM15113_ROWS, 11, 15)
HAM13 = HardHamming(HAM1393_ROWS, 9, 13)
HAM16 = HardHamming(HAM16114_ROWS, 11, 16)
HAM17 = HardHamming(HAM17123_ROWS, 12, 17)
HAM15S = SoftCode(HAM15113_ROWS, 11, 15)
HAM13S = SoftCode(HAM1393_ROWS, 9, 13)
HAM16S = SoftCode(HAM16114_ROWS, 11, 16)
SPC8S = SoftCode(tuple((0x80 >> i) | 1 for i in range(7)), 7, 8)
HAM17S = SoftCode(HAM17123_ROWS, 12, 17)
SLOTTYPE = SoftCode(GOLAY2087_ROWS, 8, 20)
EMBCODE = SoftCode(QR1676_ROWS, 7, 16)
TACT = SoftCode(TACT_ROWS, 4, 7)


def _softout(code, X, K=24, beta=2.0):
    """max-log soft output of a short block code for a batch of llr vectors X (m x n),
    restricted to the K best codewords per vector (Chase/Pyndiah style). Bits with no
    competitor among them get +-beta*|best metric gap| style saturation."""
    corr = X @ code.S.T                               # (m x 2^k)
    K = min(K, corr.shape[1])
    idx = np.argpartition(-corr, K - 1, axis=1)[:, :K]
    top = np.take_along_axis(corr, idx, axis=1)       # (m x K)
    bits = code.B[idx]                                # (m x K x n)
    big = top[:, :, None]
    m1 = np.where(bits > 0, big, -np.inf).max(axis=1)
    m0 = np.where(bits > 0, -np.inf, big).max(axis=1)
    out = 0.5 * (m1 - m0)
    best = bits[np.arange(len(X)), np.argmax(top, axis=1)]
    sat = beta * (2.0 * best - 1.0) * (np.abs(X).mean(axis=1, keepdims=True) + 1e-9)
    return np.where(np.isfinite(out), out, sat)


def tpc_decode(ch, row_code, nrows, col_code, iters=4, alpha=0.5):
    """iterative (turbo) product-code decoding, max-log. ch: (R x C) channel llrs,
    rows [0:nrows] carry row_code, every column carries col_code. Returns hard bits."""
    ext_r = np.zeros_like(ch)
    ext_c = np.zeros_like(ch)
    for _ in range(iters):
        inp = ch + alpha * ext_c
        ext_r[:nrows] = _softout(row_code, inp[:nrows]) - inp[:nrows]
        inp = ch + alpha * ext_r
        ext_c = (_softout(col_code, inp.T) - inp.T).T
    return (ch + ext_r + ext_c > 0).astype(int)


def _bptc_hard(d):
    for _ in range(5):
        fixing = False
        for c in range(15):
            col = [d[c + 1 + 15 * r] for r in range(13)]
            if HAM13.correct(col) == 1:
                for r in range(13):
                    d[c + 1 + 15 * r] = col[r]
                fixing = True
        for r in range(9):
            row = d[r * 15 + 1:r * 15 + 16]
            if HAM15.correct(row) == 1:
                d[r * 15 + 1:r * 15 + 16] = row
                fixing = True
        if not fixing:
            break
    for r in range(9):
        if HAM15.correct(d[r * 15 + 1:r * 15 + 16]) != 0:
            return False
    for c in range(15):
        if HAM13.correct([d[c + 1 + 15 * r] for r in range(13)]) != 0:
            return False
    return True


_BPTC_IDX = np.array([(a * 181) % 196 for a in range(196)])


def bptc196_decode(llr196):
    """llr196: soft bits in received (interleaved) order, positive -> 1.
    Soft turbo decoding of the 13x15 product code, then a hard clean-up / check.
    Returns 96 data bits or None."""
    L = np.asarray(llr196, dtype=np.float64)[_BPTC_IDX]
    hard = tpc_decode(L[1:].reshape(13, 15), HAM15S, 9, HAM13S)
    d = [0] + [int(v) for v in hard.reshape(-1)]
    if not _bptc_hard(d):
        d = [0] + [int(v) for v in (L[1:] > 0)]      # fall back to plain hard decoding
        if not _bptc_hard(d):
            return None
    out = d[4:12]
    for r in range(1, 9):
        out += d[r * 15 + 1:r * 15 + 12]
    return out


def crc_ccitt_dmr(bytes10):
    """CRC-CCITT as in MMDVMHost CCRC::addCCITT162 (poly 0x1021, init 0, inverted)"""
    crc = 0
    for b in bytes10:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def crc8_slc(bits28):
    """CRC-8 poly x^8+x^2+x+1, init 0, over the 28 Short LC bits"""
    reg = 0
    for b in bits28:
        fb = ((reg >> 7) & 1) ^ b
        reg = (reg << 1) & 0xFF
        if fb:
            reg ^= 0x07
    return reg


# ---------------------------------------------------------------- RS(12,9) GF(256), 0x11D
_E = [0] * 512
_L = [0] * 256
_x = 1
for _i in range(255):
    _E[_i] = _x
    _L[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _E[_i] = _E[_i - 255]


def _gm(a, b):
    return 0 if (a == 0 or b == 0) else _E[_L[a] + _L[b]]


RS_POLY = (64, 56, 14, 1)


def rs129_parity(msg9):
    par = [0, 0, 0]
    for i in range(9):
        db = msg9[i] ^ par[2]
        par[2] = par[1] ^ _gm(RS_POLY[2], db)
        par[1] = par[0] ^ _gm(RS_POLY[1], db)
        par[0] = _gm(RS_POLY[0], db)
    return [par[2], par[1], par[0]]


def rs129_check(b12, mask):
    """returns corrected 9 data bytes or None (corrects one data byte)"""
    rx = [b12[9] ^ ((mask >> 16) & 0xFF), b12[10] ^ ((mask >> 8) & 0xFF), b12[11] ^ (mask & 0xFF)]
    msg = list(b12[:9])
    p = rs129_parity(msg)
    if p == rx:
        return msg
    diff = sum(1 for i in range(3) if p[i] != rx[i])
    if diff == 1:                               # error in one parity byte
        return msg
    for i in range(9):
        orig = msg[i]
        for v in range(256):
            if v == orig:
                continue
            msg[i] = v
            if rs129_parity(msg) == rx:
                return msg
        msg[i] = orig
    return None


def bits_to_bytes(bits):
    return [bits_to_int(bits[i:i + 8]) for i in range(0, len(bits) - 7, 8)]


# ---------------------------------------------------------------- formatting
def fmt_lc(b):
    flco = b[0] & 0x3F
    fid = b[1]
    so = b[2]
    dst = (b[3] << 16) | (b[4] << 8) | b[5]
    src = (b[6] << 16) | (b[7] << 8) | b[8]
    flags = ('' if not so & 0x80 else ' EMERGENCY') + ('' if not so & 0x40 else ' ENCRYPTED')
    pf = ' (protected)' if b[0] & 0x80 else ''
    if flco == 0x00:
        return 'Group TG=%d SRC=%d%s%s' % (dst, src, flags, pf)
    if flco == 0x03:
        return 'Private TGT=%d SRC=%d%s%s' % (dst, src, flags, pf)
    return 'LC FLCO=%02X FID=%02X(%s) %s' % (flco, fid, FID_NAME.get(fid, '?'),
                                              ''.join('%02X' % x for x in b))


def _field(v, start, n, total=96):
    return (v >> (total - start - n)) & ((1 << n) - 1)


PAR_NAME = {0: 'res', 1: 'A', 2: 'B', 3: 'AB'}
ANN_NAME = {0: 'Ann_WD_TSCC', 1: 'CallTimer_Parms', 2: 'Vote_Now', 3: 'Local_Time', 4: 'MassReg',
            5: 'Chan_Freq', 6: 'Adjacent_Site', 7: 'Gen_Site_Params'}


def sys_ident(sc14, par=None):
    """14-bit Tier III system identity code -> dict + text. Net/site are shown
    +1 (ETSI user numbering, as DSD-FME does); raw field values in brackets."""
    model = (sc14 >> 12) & 3
    nb, sb = {0: (9, 3), 1: (7, 5), 2: (4, 8), 3: (2, 10)}[model]
    net = (sc14 >> sb) & ((1 << nb) - 1)
    site = sc14 & ((1 << sb) - 1)
    d = {'syscode': sc14, 'model': ['tiny', 'small', 'large', 'huge'][model], 'net': net + 1, 'site': site + 1}
    txt = 'syscode %04X (%s) Net %d Site %d' % (sc14, d['model'], net + 1, site + 1)
    if par is not None:
        d['par'] = PAR_NAME[par]
        txt += ' Cat %s' % PAR_NAME[par]
    return d, txt


def abs_channel(cont):
    """MBC continuation with CDEFTYPE 0 (absolute channel parameters) -> (lpcn, rx_hz, tx_hz)"""
    c = int.from_bytes(bytes(cont[:12]), 'big')
    if _field(c, 16, 4) != 0:
        return None
    lpcn = _field(c, 22, 12)
    tx = _field(c, 34, 10) * 1000000 + _field(c, 44, 13) * 125
    rx = _field(c, 57, 10) * 1000000 + _field(c, 67, 13) * 125
    return lpcn, rx, tx


def parse_csbk(b, cont=None):
    """b: 12 CSBK / MBC-header bytes, cont: first MBC continuation (12 bytes) or None.
    Returns (text, info dict). Field positions follow ETSI TS 102 361-4 as implemented
    in DSD-FME (dmr_csbk.c); the C_BCAST layout was confirmed on a real Tier III site."""
    v = int.from_bytes(bytes(b[:12]), 'big')
    op = b[0] & 0x3F
    fid = b[1]
    info = {'op': op, 'fid': fid}
    std = fid == 0x00 or (fid == 0x58 and op == 0x28)          # Tait sends C_BCAST with its own FID
    fids = '' if fid == 0 else ' FID=%02X(%s)' % (fid, FID_NAME.get(fid, '?'))
    name = CSBKO_NAME.get(op, 'OP_%02X' % op) if std else 'OP_%02X' % op
    tgt = _field(v, 32, 24)
    src = _field(v, 56, 24)
    if std and op == 0x19:                                      # C_ALOHA
        d, st = sys_ident(_field(v, 40, 14), _field(v, 54, 2))
        info.update(kind='aloha', site=d, reg=_field(v, 35, 1))
        return ('C_ALOHA %s reg=%d mask=%d nrandwait=%d backoff=%d%s' % (
            st, _field(v, 35, 1), _field(v, 24, 5), _field(v, 31, 4), _field(v, 36, 4), fids), info)
    if std and op == 0x28:                                      # C_BCAST
        at = _field(v, 16, 5)
        p1 = _field(v, 21, 14)
        p2 = _field(v, 56, 24)
        d, st = sys_ident(_field(v, 40, 14), _field(v, 54, 2))
        info.update(kind='bcast', ann=at, site=d, reg=_field(v, 35, 1))
        head = 'C_BCAST %s' % ANN_NAME.get(at, 'type %d' % at)
        lpcn = p2 & 0xFFF
        ch = abs_channel(cont) if cont is not None else None
        if at == 0:
            c1, c2 = (p2 >> 12) & 0xFFF, p2 & 0xFFF
            txt = '%s: TSCC LPCN %d (CC%d, %s), LPCN %d (CC%d, %s)' % (
                head, c1, (p1 >> 6) & 0xF, 'remove' if p1 & 2 else 'add',
                c2, (p1 >> 2) & 0xF, 'remove' if p1 & 1 else 'add')
            info['tscc'] = [c1, c2]
        elif at in (2, 6):
            nd, nst = sys_ident(p1)
            ava, con = (p2 >> 23) & 1, (p2 >> 22) & 1
            txt = '%s: %s, LPCN %s%s' % (head, nst, 'absolute' if lpcn == 0xFFF else lpcn,
                                         ('' if not ava else (' (online)' if con else ' (offline)')))
            info.update(neigh=nd, lpcn=lpcn)
            if ch:
                info['chan'] = ch
                txt += ' -> RX %.5f / TX %.5f MHz' % (ch[1] / 1e6, ch[2] / 1e6)
        elif at == 5:
            txt = '%s: LPCN %d' % (head, lpcn)
            info['lpcn'] = lpcn
            if ch:
                info['chan'] = (lpcn,) + ch[1:]
                txt += ' = RX %.5f MHz / TX %.5f MHz' % (ch[1] / 1e6, ch[2] / 1e6)
            else:
                txt += ' (frequency block missing)'
        elif at == 3:
            txt = '%s: %02d:%02d:%02d UTC, day %d month %d, offset %s%dh' % (
                head, (p2 >> 19) & 0x1F, (p2 >> 13) & 0x3F, (p2 >> 7) & 0x3F, (p1 >> 9) & 0x1F,
                (p1 >> 5) & 0xF, '-' if p1 & 1 else '+', (p1 >> 1) & 0xF)
        elif at == 4:
            txt = '%s: reg window %d, aloha mask %d, target %d' % (head, (p1 >> 5) & 0xF, p1 & 0x1F, p2)
        elif at == 7:
            txt = '%s: hibernate=%d tg-on-reg=%d' % (head, (p2 >> 22) & 1, (p2 >> 7) & 1)
        else:
            txt = '%s: parms1=%04X parms2=%06X' % (head, p1, p2)
        return txt + ' [%s reg=%d]%s' % (st, _field(v, 35, 1), fids), info
    if std and 0x30 <= op <= 0x38 and op != 0x38:              # channel grants
        lpcn = _field(v, 16, 12)
        ts = _field(v, 28, 1) + 1
        emerg = _field(v, 30, 1)
        info.update(kind='grant', lpcn=lpcn, ts=ts, tgt=tgt, src=src, group=op in (0x31, 0x32, 0x34, 0x38))
        txt = '%s LPCN %s TS%d %s=%d SRC=%d%s' % (name, 'absolute' if lpcn == 0xFFF else lpcn, ts,
                                                'TG' if info['group'] else 'TGT', tgt, src,
                                                ' EMERGENCY' if emerg else '')
        if lpcn == 0xFFF and cont is not None:
            ch = abs_channel(cont)
            if ch:
                info['chan'] = ch
                txt += ' -> RX %.5f MHz' % (ch[1] / 1e6)
        return txt + fids, info
    if std and op == 0x1C:                                      # C_AHOY
        sk = _field(v, 28, 4)
        info.update(kind='ahoy', tgt=tgt, src=src)
        return 'C_AHOY %s %s %s TGT=%s SRC=%s' % (AHOY_KIND.get(sk, 'kind %d' % sk),
                                               'grp' if _field(v, 25, 1) else 'ind',
                                               '(ambient listening)' if _field(v, 24, 1) else '',
                                               addr(tgt), addr(src)), info
    if std and op in (0x20, 0x21, 0x22, 0x23):                  # acknowledgements
        info.update(kind='ack', tgt=tgt, src=src)
        return '%s resp=%02X reason=%02X TGT=%s SRC=%s' % (name, _field(v, 16, 7), _field(v, 23, 8),
                                                         addr(tgt), addr(src)), info
    if std and op == 0x3D:
        info.update(kind='preamble', tgt=tgt, src=src, group=bool(b[2] & 0x40))
        return 'PREAMBLE %s %s TGT=%d SRC=%d blocks=%d' % (
            'data' if b[2] & 0x80 else 'csbk', 'grp' if b[2] & 0x40 else 'ind', tgt, src, b[3]), info
    if std and op == 0x2E:
        info.update(kind='clear', tgt=tgt, src=src)
        return 'P_CLEAR (call cleared) TGT=%d SRC=%d' % (tgt, src), info
    if std and op == 0x2F:
        return 'P_PROTECT %s kind=%d TGT=%d SRC=%d' % ('grp' if _field(v, 31, 1) else 'ind',
                                                     _field(v, 28, 3), tgt, src), info
    if std and op in (0x04, 0x05, 0x1E, 0x1F, 0x24, 0x26, 0x27, 0x2A, 0x38):
        info.update(kind='addr', tgt=tgt, src=src)
        return '%s TGT=%s SRC=%s' % (name, addr(tgt), addr(src)), info
    if fid == 0x10 and op == 0x3B:
        return 'CapPlus SYS rest=%d site=%d' % (b[2] & 0x1F, (b[3] >> 3) & 0xF), info
    return '%s%s %s' % (name, fids, ''.join('%02X' % x for x in b[2:10])), info


def fmt_slc(slco, d):
    d0, d1, d2 = (d >> 16) & 0xFF, (d >> 8) & 0xFF, d & 0xFF
    if slco == 0:
        return None                                   # null message
    if slco == 1:
        return 'SLC Activity TS1=%s TS2=%s hash %02X/%02X' % (
            ACT_NAME.get(d0 >> 4, '%X' % (d0 >> 4)), ACT_NAME.get(d0 & 0xF, '%X' % (d0 & 0xF)), d1, d2)
    if slco in (2, 3):
        kind = 'control ch (C_SYS_Parms)' if slco == 2 else 'payload ch (P_SYS_Parms)'
        extra = ' reg=%d' % ((d >> 9) & 1) if slco == 2 else ''   # CSC (a counter) left out: it would defeat dedup
        return 'SLC Tier III %s %s%s' % (kind, sys_ident(d >> 10)[1], extra)
    if slco in (9, 10):
        return 'SLC Connect Plus %s net=%d site=%d' % ('control ch' if slco == 10 else 'voice ch',
                                                      (d0 << 4) | (d1 >> 4), ((d1 & 0xF) << 4) | (d2 >> 4))
    if slco == 15:
        return 'SLC Capacity Plus rest channel LCN=%d' % d1
    return 'SLC SLCO=%d data=%06X' % (slco, d)


# ============================================================================
#  AMBE+2 voice decoding through mbelib-neo / mbelib (loaded with ctypes)
# ============================================================================
import ctypes
import ctypes.util
import os

# DMR AMBE interleave schedule (ETSI TS 102 361-1; table as in DSD / DSD-FME):
# dibit i of a 72-bit AMBE frame -> msb to ambe_fr[rW][rX], lsb to ambe_fr[rY][rZ]
_rW = (0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2)
_rX = (23, 10, 22, 9, 21, 8, 20, 7, 19, 6, 18, 5, 17, 4, 16, 3, 15, 2, 14, 1, 13, 0, 12, 10, 11, 9, 10, 8, 9, 7,
       8, 6, 7, 5, 6, 4)
_rY = (0, 2, 0, 2, 0, 2, 0, 2, 0, 3, 0, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3)
_rZ = (5, 3, 4, 2, 3, 1, 2, 0, 1, 13, 0, 12, 22, 11, 21, 10, 20, 9, 19, 8, 18, 7, 17, 6, 16, 5, 15, 4, 14, 3,
       13, 2, 12, 1, 11, 0)
AMBE_FR_IDX = np.zeros(72, dtype=np.int64)          # frame bit k -> flat index row*24+col
for _i in range(36):
    AMBE_FR_IDX[2 * _i] = _rW[_i] * 24 + _rX[_i]
    AMBE_FR_IDX[2 * _i + 1] = _rY[_i] * 24 + _rZ[_i]


def burst_ambe_llrs(llr264):
    """voice burst (264 soft bits, sync/EMB in 108..155) -> three 72-bit AMBE frames"""
    return (llr264[0:72], np.concatenate([llr264[72:108], llr264[156:192]]), llr264[192:264])


_LIB_NAMES = ('mbe-neo.dll', 'libmbe-neo.dll', 'libmbe-neo.so', 'libmbe-neo.so.2', 'libmbe-neo.dylib',
              'mbe.dll', 'libmbe.dll', 'libmbe.so', 'libmbe.so.1', 'libmbe.dylib')


def load_mbelib(path=''):
    """Returns (ctypes library, description) or (None, reason). `path` may be a file or folder."""
    cands = []
    if path:
        if os.path.isdir(path):
            cands += [os.path.join(path, n) for n in _LIB_NAMES]
        else:
            cands.append(path)
    here = [os.getcwd()]
    try:
        here.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    for d in here:
        cands += [os.path.join(d, n) for n in _LIB_NAMES]
    for n in ('mbe-neo', 'mbe'):
        f = ctypes.util.find_library(n)
        if f:
            cands.append(f)
    tried = []
    for c in cands:
        if os.path.sep in c and not os.path.exists(c):
            continue
        try:
            if hasattr(os, 'add_dll_directory') and os.path.isabs(c):
                try:
                    os.add_dll_directory(os.path.dirname(c))       # dependent DLLs next to it
                except OSError:
                    pass
            lib = ctypes.CDLL(c)
            if not hasattr(lib, 'mbe_processAmbe3600x2450Framef'):
                tried.append('%s (no AMBE+2 decoder)' % c)
                continue
            return lib, c
        except OSError as e:
            tried.append('%s (%s)' % (c, e))
    return None, ('not found; tried: ' + '; '.join(tried)) if tried else 'not found (no candidate files)'


class AmbeVocoder(object):
    """Thin ctypes wrapper. mbelib-neo 2.x: soft-decision frames + result struct.
    Classic mbelib / mbelib-neo 1.x: hard bits, (errs, errs2, err_str) signature."""
    PARMS_BYTES = 16384        # opaque mbe_parms storage (real size ~2.5 kB; never read here)

    def __init__(self, lib, uvquality=3):
        self.lib = lib
        self.neo2 = hasattr(lib, 'mbe_initProcessResult')
        self.uvq = int(uvquality)
        f = lib.mbe_processAmbe3600x2450Framef
        if self.neo2 and hasattr(lib, 'mbe_processAmbe3600x2450SoftFramef'):
            self.soft = True
            fs = lib.mbe_processAmbe3600x2450SoftFramef
            fs.restype = ctypes.c_int
            fs.argtypes = [ctypes.c_void_p] * 7
            self._fn = fs
        else:
            self.soft = False
            f.restype = ctypes.c_int if self.neo2 else None
            self._fn = f
        lib.mbe_initMbeParms.restype = None
        lib.mbe_initMbeParms.argtypes = [ctypes.c_void_p] * 3
        self.version = 'mbelib-neo 2.x (soft decision)' if self.soft else (
            'mbelib-neo 2.x (hard)' if self.neo2 else 'mbelib / mbelib-neo 1.x (hard decision)')

    def new_state(self):
        st = [ctypes.create_string_buffer(self.PARMS_BYTES) for _ in range(3)]
        self.lib.mbe_initMbeParms(st[0], st[1], st[2])
        return st

    def decode(self, state, llr72):
        """one 72-bit AMBE+2 frame (soft bits, positive -> 1) -> 160 float samples (8 kHz,
        roughly +-1 full scale) and the error count reported by the library"""
        llr72 = np.asarray(llr72, dtype=np.float64)
        bits = (llr72 > 0).astype(np.uint8)
        out = (ctypes.c_float * 160)()
        amb_d = ctypes.create_string_buffer(49)
        if self.soft:
            fr = np.zeros((96, 2), dtype=np.uint8)
            fr[AMBE_FR_IDX, 0] = bits
            fr[AMBE_FR_IDX, 1] = np.clip(np.abs(llr72) * 255.0, 1, 255).astype(np.uint8)
            res = ctypes.create_string_buffer(64)
            errs = self._fn(out, res, fr.tobytes(), amb_d, state[0], state[1], state[2])
        else:
            fr = np.zeros(96, dtype=np.uint8)
            fr[AMBE_FR_IDX] = bits
            if self.neo2:
                res = ctypes.create_string_buffer(64)
                errs = self._fn(out, res, fr.tobytes(), amb_d, state[0], state[1], state[2])
            else:
                e1, e2 = ctypes.c_int(0), ctypes.c_int(0)
                es = ctypes.create_string_buffer(64)
                self._fn(out, ctypes.byref(e1), ctypes.byref(e2), es, fr.tobytes(), amb_d,
                         state[0], state[1], state[2], ctypes.c_int(self.uvq))
                errs = e1.value + e2.value
        return np.frombuffer(out, dtype=np.float32).copy() * (7.0 / 32768.0), errs


# ============================================================================
#  GNU Radio block
# ============================================================================
_PRINT_LOCK = threading.Lock()


def _say(line):
    with _PRINT_LOCK:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


class blk(gr.decim_block):
    """DMR decoder: CACH/SLC, CSBK, voice LC, embedded LC, data headers -> text; AMBE+2 voice -> audio"""

    def __init__(self, sps=10, sync_thresh=0.80, color_code=-1, dedup_s=10.0, show_data=True,
                 invert=False, stats_every=10.0, rf_hz=0.0, plan_ref_lpcn=0, plan_ref_hz=0.0,
                 plan_step_hz=12500.0, audio_slot=0, mbelib_path='', audio_delay=0.25):
        if (int(sps) * 3) % 5:
            raise ValueError('sps must be a multiple of 5 (input rate 4800*sps -> 8 kHz audio)')
        self.adec = int(sps) * 3 // 5                 # input samples per 8 kHz audio sample
        gr.decim_block.__init__(self, name='DMR Decoder', in_sig=[np.float32],
                                out_sig=[np.float32, np.float32], decim=self.adec)
        self.message_port_register_out(pmt.intern('out'))
        self.sps = int(sps)
        self.L = 23 * self.sps + 1                    # samples spanned by a 24-symbol sync
        self.thr = float(sync_thresh)
        self.cc_filter = int(color_code)
        self.dedup_s = float(dedup_s)
        self.show_data = bool(show_data)
        self.pol = -1.0 if invert else 1.0
        self.stats_every = float(stats_every)
        self.tm = {}
        self.tsyms = {}
        for k, v in SYNCS.items():
            s = _fs_syms(v)
            self.tsyms[k] = s
            t = s - s.mean()
            self.tm[k] = t / np.sqrt(np.sum(t * t))
        t = _fs_syms(P25_FS)
        t = t - t.mean()
        self.tm_p25 = t / np.sqrt(np.sum(t * t))
        self.buf = np.zeros(0)
        self.base = 0                  # absolute sample index of buf[0]
        self.scan = 0                  # first absolute window start not yet finalised
        self.jobs = []                 # (abs sync pos [samples, float], kind, voice, n, fit)
        self.done = {}                 # kind -> list of recent hit positions
        self.fit = None                # last (g, o)
        self.phase = 0.0               # last sync timing, samples mod sps (display output)
        self.last_bs = None
        self.grid = None               # abs sample position of the next CACH grid point
        self.slc_bits = []
        self.emb = {}
        self.seen = {}
        self.n = {'bursts': 0, 'bad_slot_type': 0, 'bptc_fail': 0, 'crc_fail': 0, 'voice': 0,
                  'idle': 0, 'p25': 0, 'offgrid': 0}
        self.ccs = {}
        self.grid_ok, self.cand = False, []
        self.t_last = time.time()
        self._spc4 = SoftCode((0b1001, 0b0101, 0b0011), 3, 4)
        # ---- channel picture for the status panel
        self.rf_hz = float(rf_hz)
        self.plan_ref_lpcn = int(plan_ref_lpcn)
        self.plan_ref_hz = float(plan_ref_hz)
        self.plan_step_hz = float(plan_step_hz)
        self._rf_seen = self.rf_hz
        self.t0 = time.time()
        # ---- voice
        self.audio_slot = int(audio_slot)             # 0 = all slots, 1 = TS1, 2 = TS2
        self.audio_delay = float(audio_delay)
        self.voc, self.voc_msg = None, ''
        lib, where = load_mbelib(str(mbelib_path))
        if lib is not None:
            try:
                self.voc = AmbeVocoder(lib)
                self.voc_gain = 1.0 if self.voc.neo2 else 0.33      # classic mbelib is ~3x louder
                self.voc_msg = '%s  [%s]' % (self.voc.version, where)
            except Exception as e:                    # wrong / incompatible library
                self.voc_msg = 'library found but unusable: %s (%s)' % (where, e)
        else:
            self.voc_msg = 'no AMBE+2 vocoder (mbe-neo.dll / mbelib) %s' % where
        _say('[DMR] voice: ' + (self.voc_msg if self.voc else self.voc_msg + '  -> audio output stays silent'))
        self.vstate = {}                              # slot -> mbelib state triplet
        self.vlast = {}                               # slot -> sample position of last voice burst
        # audio timeline: each burst's 60 ms is placed audio_delay after the burst itself, so the
        # output is paced by the signal (pauses and lost bursts keep their real length)
        self.abuf = np.zeros(int(8000 * (self.audio_delay + 1.0)), dtype=np.float32)
        self.abase = 0                                # output sample index of abuf[0]
        self.alate = 0
        try:                                          # bound the chunk size: the delay must cover it
            self.set_max_noutput_items(max(80, int(8000 * self.audio_delay * 0.4)))
        except Exception:
            pass
        self.enc = {}                                 # slot -> current call encrypted
        self.vstats = {}                              # slot -> [frames, errors]
        self.enc_note = {}
        self.mbc = {}                  # slot key -> [header bytes, continuation list, time]
        self.site = None               # (text, time) of this site's identity
        self.chan_kind = {}            # description -> last time seen
        self.fids = {}
        self.slot_act = {}             # 'TS1'/'TS2'/... -> (time, text)
        self.chmap = {}                # LPCN -> (rx_hz, tx_hz)
        self.neigh = {}                # syscode -> (text, lpcn, time)
        self.calls = {}                # (kind, tg/target, src) -> [first, last, where]
        self.offs = []                 # recent (off_hz, dev_hz)

    # ------------------------------------------------------------------ output
    def _emit(self, key, msg, tail=''):
        now = time.time()
        if self.dedup_s > 0:
            last = self.seen.get(key + msg)
            self.seen[key + msg] = now
            if last is not None and now - last < self.dedup_s:
                return
            if len(self.seen) > 5000:
                self.seen = {k: v for k, v in self.seen.items() if now - v < self.dedup_s}
        line = '[DMR %s] %s %s%s' % (time.strftime('%H:%M:%S'), key, msg, tail)
        _say(line)
        b = np.frombuffer((line + '\n').encode('ascii', 'replace'), dtype=np.uint8)
        self.message_port_pub(pmt.intern('out'), pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(b), b.tolist())))

    # ------------------------------------------------------------------ sampling
    def _rho(self, seg, tmpl):
        """normalised correlation of every sample offset with a 24-symbol template"""
        w = np.lib.stride_tricks.sliding_window_view(seg, self.L)[:, ::self.sps]
        wc = w - w.mean(axis=1, keepdims=True)
        den = np.sqrt(np.einsum('ij,ij->i', wc, wc)) + 1e-12
        return (wc @ tmpl) / den

    def _syms(self, pos, s0, cnt):
        """symbols at absolute sample position pos + sps*(s0 .. s0+cnt-1), cubic interp."""
        t = pos - self.base + self.sps * (s0 + np.arange(cnt))
        i = np.floor(t).astype(int)
        f = t - i
        if i[0] - 1 < 0 or i[-1] + 2 >= len(self.buf):
            return None
        b = self.buf
        ym1, y0, y1, y2 = b[i - 1], b[i], b[i + 1], b[i + 2]
        # Catmull-Rom
        return y0 + 0.5 * f * (y1 - ym1 + f * (2 * ym1 - 5 * y0 + 4 * y1 - y2 + f * (3 * (y0 - y1) + y2 - ym1)))

    # ------------------------------------------------------------------ CACH
    @staticmethod
    def _cach(y, g, o):
        """y: 144 symbols (index 66 = sync start). Returns (slot 0/1, lcss, payload llrs) or None"""
        z = (y[0:12] - o) / g
        llr = syms_to_llr(z)
        tact, nerr = TACT.decode(llr[list(CACH_TACT_POS)])
        if nerr > 1:
            return None
        return (tact >> 2) & 1, tact & 3, [float(llr[p]) for p in CACH_PAYLOAD_POS]

    def _slc_feed(self, lcss, payload):
        if lcss == 1:
            self.slc_bits = list(payload)
        elif lcss == 3:
            if self.slc_bits and len(self.slc_bits) < 51:
                self.slc_bits += payload
            else:
                self.slc_bits = []
        elif lcss == 2:
            if len(self.slc_bits) == 51:
                self.slc_bits += payload
                self._slc_decode(self.slc_bits)
            self.slc_bits = []
        else:
            self.slc_bits = []

    def _slc_decode(self, raw):
        L = np.zeros(68)
        for a in range(67):
            L[a] = raw[(a * 4) % 67]
        L[67] = raw[67]
        d = [int(v) for v in tpc_decode(L.reshape(4, 17), HAM17S, 3, self._spc4).reshape(-1)]
        for r in range(3):
            if HAM17.correct(d[17 * r:17 * r + 17]) != 0:
                return
        for c in range(17):
            if d[51 + c] != (d[c] ^ d[17 + c] ^ d[34 + c]):
                return
        bits = d[0:12] + d[17:29] + d[34:46]
        if crc8_slc(bits[:28]) != bits_to_int(bits[28:36]):
            return
        slco, dat = bits_to_int(bits[:4]), bits_to_int(bits[4:28])
        txt = fmt_slc(slco, dat)
        if txt:
            self._emit('CACH', txt)
        now = time.time()
        if slco in (2, 3):
            if not (self.site and now - self.site[1] < 5):   # prefer ALOHA text (has Cat)
                self.site = (sys_ident(dat >> 10)[1], now, dat >> 10)
            self._kind('Tier III ' + ('CONTROL channel (TSCC)' if slco == 2 else 'traffic (payload) channel'))
        elif slco in (9, 10):
            self._kind('Connect Plus ' + ('control' if slco == 10 else 'voice') + ' channel')
        elif slco == 15:
            self._kind('Capacity Plus')
        elif slco == 1:
            a1, a2 = dat >> 20, (dat >> 16) & 0xF
            self._act('TS1', 'SLC activity: ' + ACT_NAME.get(a1, '%X' % a1), weak=True)
            self._act('TS2', 'SLC activity: ' + ACT_NAME.get(a2, '%X' % a2), weak=True)

    def _grid_run(self, end_abs):
        """walk the repeater burst grid (every 144 symbols) collecting Short LC"""
        if self.grid is None or self.fit is None:
            return
        g, o = self.fit
        step = 144 * self.sps
        while self.grid + 80 * self.sps <= end_abs:
            if self.grid - self.last_bs > 34 * step:          # repeater gone (> ~1 s)
                self.grid = None
                return
            y = self._syms(self.grid, -66, 144)
            r = self._cach(y, g, o) if y is not None else None
            if r is not None:
                self._slc_feed(r[1], r[2])
            else:
                self.slc_bits = []
            self.grid += step

    # ------------------------------------------------------------------ bursts
    def _fit(self, y, kind, voice):
        t = self.tsyms[kind] * (1.0 if voice else -1.0)       # g < 0 if the receiver inverts
        A = np.column_stack([t, np.ones(24)])
        g, o = np.linalg.lstsq(A, y[66:90], rcond=None)[0]
        if abs(g) < 1e-6:
            return None
        yb = y[12:144]
        lev = DIBIT_LEVEL[level_to_dibit((yb - o) / g)]
        A = np.column_stack([lev, np.ones(len(lev))])
        g2, o2 = np.linalg.lstsq(A, yb, rcond=None)[0]
        if abs(g2) > 0.5 * abs(g):
            return g2, o2
        return g, o

    def _slot_key(self, y, kind, g, o):
        if kind == 'BS':
            r = self._cach(y, g, o)
            return 'TS%d' % (r[0] + 1) if r is not None else 'TS?'
        return {'DM1': 'DM-TS1', 'DM2': 'DM-TS2'}.get(kind, 'MS')

    def _data_burst(self, y, kind, g, o):
        z = (y[12:144] - o) / g
        llr = syms_to_llr(z)
        st, nerr = SLOTTYPE.decode(np.concatenate([llr[98:108], llr[156:166]]))
        if nerr > 3:
            self.n['bad_slot_type'] += 1
            return
        cc, dt = st >> 4, st & 0xF
        if self.cc_filter >= 0 and cc != self.cc_filter:
            return
        if nerr == 0:                                # count colour codes from clean slot types only
            self.ccs[cc] = self.ccs.get(cc, 0) + 1
        slot = self._slot_key(y, kind, g, o)
        key = '%s CC%d' % (slot, cc)
        tail = '  (dev=%.0fHz off=%+.0fHz)' % (abs(g) * 1944.0, o * 648.0)
        self.offs = (self.offs + [(o * 648.0, abs(g) * 1944.0)])[-50:]
        self._kind({'BS': 'repeater / base station (BS sync)', 'MS': 'mobile / talk-around (MS sync)'}.get(
            kind, 'direct mode (DM sync)'))
        if dt == 9 and nerr == 0:
            self._act(slot, 'idle')
        if dt == 9:
            self.n['idle'] += 1
            return
        if dt in (7, 8, 10, 5) and not self.show_data:
            return
        info = np.concatenate([llr[0:98], llr[166:264]])
        if dt == 8:
            self._emit(key, 'DATA 3/4 block')
            return
        if dt in (11, 12, 13, 14, 15):
            self._emit(key, 'slot type %d (reserved)' % dt, tail)
            return
        bits = bptc196_decode(info)
        if bits is None:
            self.n['bptc_fail'] += 1
            return
        b = bits_to_bytes(bits)
        if dt in RS_MASK:
            lc = rs129_check(b, RS_MASK[dt])
            if lc is None:
                self.n['crc_fail'] += 1
                return
            txt = fmt_lc(lc)
            if dt == 1 and (lc[0] & 0x3F) in (0x00, 0x03):
                self.enc[slot] = bool(lc[2] & 0x40)
                self.enc_note[slot] = False
            elif dt == 2:
                self.enc[slot] = False
            self._emit(key, '%s %s' % ('CALL' if dt == 1 else 'END ', txt), tail)
            self._call(key.split()[0], txt, 'voice here' if dt == 1 else 'ended')
            return
        if dt == 5:                                  # MBC continuation (unmasked CRC)
            self._mbc_cont(b, key, tail)
            return
        if dt in CRC_MASK:
            crc = (b[10] << 8 | b[11]) ^ CRC_MASK[dt]
            if crc_ccitt_dmr(b[:10]) != crc:
                self.n['crc_fail'] += 1
                return
            if dt == 3:
                self._csbk(b, None, key, tail)
            elif dt == 0:
                self._act(slot, 'PI header (encrypted call)')
                self.enc[slot] = True
                self._emit(key, 'PI HDR ALG=%02X KID=%02X MI=%08X TGT=%d' % (
                    b[0], b[2], (b[3] << 24) | (b[4] << 16) | (b[5] << 8) | b[6],
                    (b[7] << 16) | (b[8] << 8) | b[9]), tail)
            elif dt == 6:
                self._act(slot, 'data')
                dpf = b[0] & 0xF
                self._emit(key, 'DATA HDR %s %s SAP=%d DST=%d SRC=%d' % (
                    DPF_NAME.get(dpf, 'DPF%d' % dpf), 'grp' if b[0] & 0x80 else 'ind', b[1] >> 4,
                    (b[2] << 16) | (b[3] << 8) | b[4], (b[5] << 16) | (b[6] << 8) | b[7]), tail)
            elif dt == 4:                            # wait for the continuation block(s)
                self.mbc[key] = [list(b), [], time.time()]
            return
        self._act(slot, DT_NAME.get(dt, 'type %d' % dt).lower())
        if self.show_data:
            self._emit(key, '%s block' % DT_NAME.get(dt, dt))

    def _emb_burst(self, y, skey, g, o):
        """voice burst B..F: y = 144 symbols, EMB/embedded LC at 66..90"""
        z = (y[66:90] - o) / g
        llr = syms_to_llr(z)
        e, nerr = EMBCODE.decode(np.concatenate([llr[0:8], llr[40:48]]))
        if nerr > 2:
            self.emb[skey] = []
            return
        cc, lcss = e >> 3, e & 3
        frag = list(llr[8:40])
        lst = self.emb.get(skey, [])
        if lcss == 1:
            lst = [frag]
        elif lcss == 3 and 1 <= len(lst) <= 2:
            lst.append(frag)
        elif lcss == 2 and len(lst) == 3:
            lst.append(frag)
            self._emb_lc(sum(lst, []), skey, cc)
            lst = []
        else:
            lst = []
        self.emb[skey] = lst

    def _emb_lc(self, raw, skey, cc):
        L = np.zeros(128)
        b = 0
        for a in range(128):
            L[b] = raw[a]
            b += 16
            if b > 127:
                b -= 127
        d = [int(v) for v in tpc_decode(L.reshape(8, 16), HAM16S, 7, SPC8S).reshape(-1)]
        for r in range(7):
            if HAM16.correct(d[16 * r:16 * r + 16]) != 0:
                return
        for c in range(16):
            if (d[c] ^ d[16 + c] ^ d[32 + c] ^ d[48 + c] ^ d[64 + c] ^ d[80 + c] ^ d[96 + c] ^ d[112 + c]):
                return
        data = d[0:11] + d[16:27] + d[32:42] + d[48:58] + d[64:74] + d[80:90] + d[96:106]
        crc = (d[42] << 4) | (d[58] << 3) | (d[74] << 2) | (d[90] << 1) | d[106]
        lc = bits_to_bytes(data[:72])
        if sum(lc) % 31 != crc:
            return
        txt = fmt_lc(lc)
        if (lc[0] & 0x3F) in (0x00, 0x03):
            self.enc[skey] = bool(lc[2] & 0x40)
        self._emit('%s CC%d' % (skey, cc), 'CALL %s' % txt)
        self._call(skey, txt, 'voice here')

    # ------------------------------------------------------------------ channel picture
    def _reset_channel(self):
        """forget everything that describes 'this channel' (called on retune)"""
        self.t0 = time.time()
        self.mbc, self.emb, self.slc_bits = {}, {}, []
        self.site, self.chan_kind, self.fids = None, {}, {}
        self.slot_act, self.neigh, self.calls, self.offs = {}, {}, {}, []
        self.ccs = {}
        self.grid, self.last_bs, self.fit = None, None, None
        self.grid_ok, self.cand = False, []
        self.jobs = []
        self.n = {k: 0 for k in self.n}
        self.vstate, self.vlast, self.enc, self.vstats = {}, {}, {}, {}

    def _kind(self, k):
        self.chan_kind[k] = time.time()

    def _act(self, slot, txt, weak=False):
        if slot == 'TS?':
            return
        now = time.time()
        old = self.slot_act.get(slot)
        if weak and old is not None and now - old[0] < 2.0 and not old[1].startswith('SLC'):
            return                                   # a decoded burst beats the CACH summary
        if txt == 'voice' and old is not None and old[1].startswith('voice') and now - old[0] < 2.0:
            txt = old[1]                             # keep the talkgroup text during a call
        self.slot_act[slot] = (now, txt)

    def _call(self, slot, lc_txt, where):
        now = time.time()
        k = lc_txt.split(' ENCRYPTED')[0].split(' EMERGENCY')[0]
        c = self.calls.setdefault(k, [now, now, where])
        c[1], c[2] = now, (where if where != 'voice here' else '%s on %s' % (where, slot))
        if where == 'voice here':
            self.slot_act[slot] = (now, 'voice ' + lc_txt)

    def _plan(self, lpcn):
        if self.plan_ref_hz > 0 and lpcn not in (0, 0xFFF):
            return self.plan_ref_hz + (lpcn - self.plan_ref_lpcn) * self.plan_step_hz
        return None

    def _freq(self, lpcn):
        if lpcn in self.chmap:
            return '%.5f MHz' % (self.chmap[lpcn][0] / 1e6)
        f = self._plan(lpcn)
        if f is not None:
            return 'LPCN %d = %.5f MHz (plan)' % (lpcn, f / 1e6)
        return 'LPCN %d (freq not announced)' % lpcn

    def _csbk(self, b, cont, key, tail):
        txt, info = parse_csbk(b, cont)
        now = time.time()
        fid = info['fid']
        self.fids[fid] = now
        k = info.get('kind')
        slot = key.split()[0]
        if 'chan' in info and info['chan'][0] not in (0, 0xFFF):
            self.chmap[info['chan'][0]] = info['chan'][1:]
        if k in ('aloha', 'bcast'):
            self.site = (sys_ident(info['site']['syscode'], {v: k for k, v in PAR_NAME.items()}.get(info['site'].get('par')))[1],
                         now, info['site']['syscode'])
            self._kind('Tier III CONTROL channel (TSCC)')
            self._act(slot, 'control signalling (%s)' % ('C_ALOHA' if k == 'aloha' else 'C_BCAST'))
            if 'neigh' in info and info.get('lpcn') not in (None, 0, 0xFFF) and 'chan' not in info \
                    and self._plan(info['lpcn']):
                txt += ' -> %s' % self._freq(info['lpcn'])
            if 'neigh' in info:
                nd = info['neigh']
                self.neigh[nd['syscode']] = ('Net %d Site %d' % (nd['net'], nd['site']), info.get('lpcn'), now,
                                             ANN_NAME.get(info['ann'], ''), info.get('chan'))
        elif k == 'grant':
            where = 'granted %s TS%d' % (
                ('%.5f MHz' % (info['chan'][1] / 1e6)) if 'chan' in info else self._freq(info['lpcn']), info['ts'])
            ltxt = ('Group TG=%d SRC=%d' if info['group'] else 'Private TGT=%d SRC=%d') % (info['tgt'], info['src'])
            self._call(slot, ltxt, where)
            self._act(slot, 'control signalling (grants)')
            if 'chan' not in info and (info['lpcn'] in self.chmap or self._plan(info['lpcn'])):
                txt += ' -> %s' % self._freq(info['lpcn'])
        elif k:
            self._act(slot, 'CSBK ' + txt.split()[0])
        self._emit(key, txt, tail)

    def _mbc_cont(self, b, key, tail):
        m = self.mbc.get(key)
        if m is None or time.time() - m[2] > 1.0:
            return
        m[1].append(list(b))
        if not (b[0] >> 7) & 1 and len(m[1]) < 3:      # more continuation blocks follow
            return
        del self.mbc[key]
        bits = []
        for blk_ in m[1]:
            for x in blk_:
                bits += int_bits(x, 8)
        if crc_ccitt_dmr(bits_to_bytes(bits[:-16])) != bits_to_int(bits[-16:]):
            self.n['crc_fail'] += 1
            return
        self._csbk(m[0], m[1][0], key, tail)

    def _status(self, now):
        L = []
        bar = '=' * 72
        L.append(bar)
        L.append(' DMR channel status   %s   (running %d s)' % (time.strftime('%H:%M:%S'), now - self.t0))
        L.append(bar)
        rf = ('%.5f MHz' % (self.rf_hz / 1e6)) if self.rf_hz > 0 else '(set rf_hz)'
        if self.offs:
            o = sorted(x[0] for x in self.offs)[len(self.offs) // 2]
            d = sorted(x[1] for x in self.offs)[len(self.offs) // 2]
            ppm = ('  ~%+.1f ppm' % (o / self.rf_hz * 1e6)) if self.rf_hz > 0 else ''
            L.append(' Frequency   : %s   carrier offset %+.0f Hz%s   deviation %.0f Hz' % (rf, o, ppm, d))
        else:
            L.append(' Frequency   : %s   (no DMR burst decoded yet)' % rf)
        kinds = [k for k, t in sorted(self.chan_kind.items(), key=lambda kv: -kv[1]) if now - t < 30]
        L.append(' Channel type: %s' % ('; '.join(kinds) if kinds else '-'))
        if self.site and now - self.site[1] < 60:
            L.append(' Site        : %s' % self.site[0])
        vend = ['%02X %s' % (f, FID_NAME.get(f, '?')) for f, t in self.fids.items() if now - t < 60 and f != 0]
        if self.fids:
            L.append(' Feature IDs : standard%s' % ((' + ' + ', '.join(vend)) if vend else ''))
        cc = sorted(self.ccs.items(), key=lambda kv: -kv[1])
        L.append(' Colour code : %s' % (', '.join('CC%d (%d bursts)' % kv for kv in cc) if cc else '-'))
        for sl in sorted(self.slot_act):
            t, txt = self.slot_act[sl]
            age = now - t
            L.append(' %-12s: %s%s' % (sl, txt if age < 5 else 'silent', '' if age < 5 else ' (last: %s, %d s ago)' % (txt, age)))
        if self.neigh:
            L.append(' Neighbours  :')
            own = self.site[2] if self.site and len(self.site) > 2 else None
            for sc, (txt, lpcn, t, how, ch) in sorted(self.neigh.items()):
                f = ('%.5f MHz' % (ch[1] / 1e6)) if ch else (self._freq(lpcn) if lpcn not in (None, 0xFFF) else '-')
                L.append('    %-20s syscode %04X  TSCC %-36s [%s, %d s ago]%s' % (
                    txt, sc, f, how, now - t, '  <- THIS SITE' if sc == own else ''))
        if self.plan_ref_hz > 0:
            L.append(' Channel plan: (configured) LPCN n = %.5f MHz + (n - %d) x %.3f kHz' % (
                self.plan_ref_hz / 1e6, self.plan_ref_lpcn, self.plan_step_hz / 1e3))
        if self.chmap:
            L.append(' Announced   : ' + ', '.join('LPCN %d = %.5f MHz (TX %.5f)' % (k, v[0] / 1e6, v[1] / 1e6)
                                              for k, v in sorted(self.chmap.items())))
        recent = sorted(((v[1], k, v) for k, v in self.calls.items() if now - v[1] < 300), reverse=True)[:8]
        if recent:
            L.append(' Calls (last 5 min, newest first):')
            for t, k, v in recent:
                L.append('    %s  %-36s %s' % (time.strftime('%H:%M:%S', time.localtime(t)), k, v[2]))
        vl = ' Voice audio : %s' % (self.voc_msg if self.voc else 'OFF - ' + self.voc_msg)
        L.append(vl[:150])
        if self.voc:
            L.append('               playing: %s   delay %.2f s%s   frames decoded/avg bit errors: %s' % (
                {0: 'all slots', 1: 'TS1', 2: 'TS2'}.get(self.audio_slot, '?'), self.audio_delay,
                ('  (%d bursts too late: raise audio_delay)' % self.alate) if self.alate else '',
                ', '.join('%s %d/%.2f%s' % (k, v[0], v[1] / max(v[0], 1), ' (encrypted, muted)' if self.enc.get(k) else '')
                          for k, v in sorted(self.vstats.items())) or '-'))
        L.append(' Decoder     : bursts %d (idle %d, voice superframes %d)  rejected: slot-type %d, BPTC %d, CRC/RS %d, off-grid/unconfirmed sync %d'
                 % (self.n['bursts'], self.n['idle'], self.n['voice'], self.n['bad_slot_type'],
                    self.n['bptc_fail'], self.n['crc_fail'], self.n['offgrid']))
        if self.n['p25'] > 5 and self.n['bursts'] == 0:
            L.append(' NOTE        : P25 syncs seen (%d) -> use p25_rx' % self.n['p25'])
        elif self.n['bursts'] == 0:
            L.append(' NOTE        : no DMR sync: check frequency/ppm')
        elif self.n['bad_slot_type'] > self.n['bursts'] // 2:
            L.append(' NOTE        : mostly bad slot types: try invert=True or fix ppm')
        L.append(bar)
        return '\n'.join(L)

    # ------------------------------------------------------------------ burst grid
    def _on_grid(self, p, q):
        k = (p - q) / (144.0 * self.sps)
        return abs(k - round(k)) <= 0.05

    def _grid_check(self, pos):
        """A repeater sends syncs only on a 30 ms (144 symbol) grid. Accept a BS sync if it is on
        the grid of the last accepted one (< 0.5 s ago); otherwise only once a second sync
        within 1 s confirms its grid. Rejects AMBE/data payload that happens to look like a sync."""
        sps = self.sps
        if self.grid_ok and self.last_bs is not None and pos - self.last_bs < 0.5 * 4800 * sps:
            if self._on_grid(pos, self.last_bs):
                return True
            self.n['offgrid'] += 1
            return False
        self.grid_ok = False
        self.cand = [c for c in self.cand if pos - c < 4800 * sps]
        if any(self._on_grid(pos, c) for c in self.cand):
            self.grid_ok = True
            self.cand = []
            return True
        self.cand.append(pos)
        self.n['offgrid'] += 1
        return False

    # ------------------------------------------------------------------ voice
    def _slot_selected(self, slot):
        if self.audio_slot == 0:
            return True
        return slot.endswith('TS%d' % self.audio_slot) or (self.audio_slot == 1 and slot == 'MS')

    def _voice(self, y, g, o, slot, pos):
        """decode the three AMBE+2 frames of one voice burst and queue the audio"""
        if self.voc is None or slot == 'TS?':
            return
        gap = 3 * 288 * self.sps
        if slot not in self.vstate or pos - self.vlast.get(slot, -10 ** 12) > gap:
            self.vstate[slot] = self.voc.new_state()             # new call: fresh vocoder state
        self.vlast[slot] = pos
        llr = syms_to_llr((y[12:144] - o) / g)
        pcm = []
        st = self.vstats.setdefault(slot, [0, 0])
        for f in burst_ambe_llrs(llr):
            a, e = self.voc.decode(self.vstate[slot], f)
            pcm.append(a)
            st[0] += 1
            st[1] += max(int(e), 0)
        if self.enc.get(slot):
            if not self.enc_note.get(slot):
                self.enc_note[slot] = True
                _say('[DMR %s] %s encrypted call: audio muted' % (time.strftime('%H:%M:%S'), slot))
            return
        if self._slot_selected(slot):
            a = np.concatenate(pcm) * self.voc_gain
            start = int(round((pos + self.audio_delay * 4800 * self.sps) / self.adec)) - self.abase
            if start < 0:                                       # decoded too late for its slot
                self.alate += 1
                a, start = a[-start:], 0
            end = start + len(a)
            if end > len(self.abuf):
                self.abuf = np.concatenate([self.abuf, np.zeros(end - len(self.abuf) + 8000, np.float32)])
            self.abuf[start:end] += a

    def _audio_out(self, n):
        if len(self.abuf) < n:
            self.abuf = np.concatenate([self.abuf, np.zeros(n - len(self.abuf), np.float32)])
        out = np.clip(self.abuf[:n].copy(), -1.0, 1.0)
        self.abuf = np.concatenate([self.abuf[n:], np.zeros(n, np.float32)])
        self.abase += n
        return out

    # ------------------------------------------------------------------ sync search
    def _search(self, end):
        sps, L = self.sps, self.L
        h = sps // 2                                   # +- half a symbol = one sync
        lo = max(self.scan - h, self.base)             # left context for peak picking
        last_final = end - L - h                       # last window start that can be finalised
        if last_final < self.scan:
            return
        seg = self.buf[lo - self.base:]
        nwin = len(seg) - L + 1
        if nwin <= 0:
            return
        for kind, tm in self.tm.items():
            rho = self.pol * self._rho(seg, tm)
            ar = np.abs(rho)
            for k in np.flatnonzero(ar >= self.thr):
                a = lo + int(k)
                if a < self.scan or a > last_final:
                    continue
                k0, k1 = max(k - h, 0), min(k + h + 1, nwin)
                if ar[k] < ar[k0:k1].max():
                    continue
                if any(abs(a - p) <= h for p in self.done.get(kind, [])):
                    continue
                # fractional peak (parabola through k-1, k, k+1)
                d = 0.0
                if 0 < k < nwin - 1:
                    den = ar[k - 1] - 2 * ar[k] + ar[k + 1]
                    if den < 0:
                        d = float(np.clip(0.5 * (ar[k - 1] - ar[k + 1]) / den, -0.5, 0.5))
                self.done.setdefault(kind, []).append(a)
                self.jobs.append((a + d, kind, bool(rho[k] > 0), 0, None))
        rp = np.abs(self._rho(seg, self.tm_p25))
        self.n['p25'] += int(np.sum((rp[1:-1] >= 0.9) & (rp[1:-1] >= rp[:-2]) & (rp[1:-1] >= rp[2:])) // sps)
        for kind in self.done:
            self.done[kind] = [p for p in self.done[kind] if p > end - 4 * L - 40000]
        self.scan = last_final + 1

    # ------------------------------------------------------------------ work
    def work(self, input_items, output_items):
        x = input_items[0]
        aout, out = output_items[0], output_items[1]
        sps = self.sps
        n_out = len(out)
        x = x[:n_out * self.adec].astype(np.float64)
        if self.rf_hz != self._rf_seen:                # retuned (GRC callback set rf_hz)
            self._rf_seen = self.rf_hz
            self._reset_channel()
            if time.time() - getattr(self, '_t_reset_msg', 0) > 1.0:   # a slider drag = many steps
                self._t_reset_msg = time.time()
                _say('[DMR %s] --- retuned to %.5f MHz: channel picture reset ---' % (
                    time.strftime('%H:%M:%S'), self.rf_hz / 1e6))
        abs0 = self.base + len(self.buf)               # absolute index of x[0]
        self.buf = np.concatenate([self.buf, x])
        end = self.base + len(self.buf)
        self._search(end)

        self.jobs.sort(key=lambda j: j[0])
        still = []
        for job in self.jobs:
            pos, kind, voice, n, fit = job
            if pos + 80 * sps + 3 > end:
                still.append(job)
                continue
            if n == 6:                                 # predicted next superframe (burst A)
                h = sps // 2 + 1
                if any(abs(pos - p) <= h for p in self.done.get(kind, [])):
                    continue                           # the sync search found it: that job handles it
                y = self._syms(pos, -66, 144)
                if y is None:
                    continue
                t = self.tsyms[kind]
                yc = y[66:90] - y[66:90].mean()
                rho = float(np.dot(yc, t - t.mean()) / (np.linalg.norm(yc) * np.linalg.norm(t - t.mean()) + 1e-12))
                if rho < 0.5:
                    continue                           # no voice sync there: the call has ended
                n = 0                                  # weak sync: decode it as burst A
            if kind != 'BS' and n == 0 and self.last_bs is not None and \
                    abs(pos - self.last_bs) < 2 * 4800 * sps:
                continue                               # repeater locked: MS/DM syncs here are false hits
            if kind == 'BS' and n == 0 and not self._grid_check(pos):
                continue                               # not (yet) on a confirmed 30 ms burst grid
            y = self._syms(pos, -66, 144)
            if y is None:
                continue
            self.n['bursts'] += 1
            if n == 0:
                gf = self._fit(y, kind, voice)
                if gf is None:
                    continue
                g, o = gf
                self.fit = (g, o)
                self.phase = pos % sps
                if kind == 'BS':
                    step = 144 * sps
                    if self.grid is None or abs(((pos - self.grid) / step) - round((pos - self.grid) / step)) > 0.05:
                        self.grid = pos
                    else:                               # keep the grid, refresh its timing
                        self.grid = pos + step * round((self.grid - pos) / step)
                    self.last_bs = pos
                if voice:
                    self.n['voice'] += 1
                    skey = self._slot_key(y, kind, g, o)
                    self._act(skey, 'voice')
                    self._voice(y, g, o, skey, pos)
                    for k in range(1, 7):
                        still.append((pos + 288 * sps * k, kind, True, k, (g, o, skey)))
                else:
                    self._data_burst(y, kind, g, o)
            else:
                g, o, skey = fit
                self._emb_burst(y, skey, g, o)
                self._voice(y, g, o, skey, pos)
        self.jobs = still
        self._grid_run(end)

        # display output: symbol-centre samples, held at the 8 kHz output rate.
        # self.phase is an absolute sample position (mod sps); convert it to this chunk,
        # whose first sample is at absolute index abs0 (chunks are multiples of 6, not of sps)
        ph = int(round(self.phase - abs0)) % sps
        t = np.arange(n_out) * self.adec
        idx = ph + np.floor((t - ph) / sps).astype(int) * sps
        idx[idx < 0] = ph
        out[:] = x[np.minimum(idx, len(x) - 1)]
        aout[:] = self._audio_out(n_out)

        keep = min([self.scan - sps] + [j[0] for j in self.jobs] +
                   ([self.grid] if self.grid is not None else []))
        cut = int(keep - 70 * sps) - self.base
        if cut > 0:
            self.buf = self.buf[cut:]
            self.base += cut

        now = time.time()
        if self.stats_every > 0 and now - self.t_last >= self.stats_every:
            self.t_last = now
            _say(self._status(now))
        return n_out
