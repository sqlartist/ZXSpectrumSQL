#!/usr/bin/env python3
"""
z80dis.py -- a Z80 disassembler, built from the same x/y/z decomposition
as the SQL core so the two agree about what an opcode means.

    from z80dis import disassemble
    for line in disassemble(blob, org=23813):
        print(line)
"""

R = ['B', 'C', 'D', 'E', 'H', 'L', '(HL)', 'A']
RP = ['BC', 'DE', 'HL', 'SP']
RP2 = ['BC', 'DE', 'HL', 'AF']
CC = ['NZ', 'Z', 'NC', 'C', 'PO', 'PE', 'P', 'M']
ALU = ['ADD A,', 'ADC A,', 'SUB ', 'SBC A,', 'AND ', 'XOR ', 'OR ', 'CP ']
ROT = ['RLC', 'RRC', 'RL', 'RR', 'SLA', 'SRA', 'SLL', 'SRL']
AF_ = ["EX AF,AF'"]


class Reader:
    def __init__(self, buf, pos, org=0):
        self.b, self.p, self.org = buf, pos, org

    def u8(self):
        v = self.b[self.p] if self.p < len(self.b) else 0
        self.p += 1
        return v

    def s8(self):
        v = self.u8()
        return v - 256 if v > 127 else v

    def u16(self):
        lo, hi = self.u8(), self.u8()
        return lo | (hi << 8)


def _hl(ix, d=None):
    if ix is None:
        return 'HL'
    return ix


def _r(i, ix, rd):
    """Register operand, honouring an active DD/FD prefix."""
    if ix is None:
        return R[i]
    if i == 6:
        d = rd.s8()
        return f'({ix}{d:+d})'
    if i == 4:
        return ix + 'h'
    if i == 5:
        return ix + 'l'
    return R[i]


def decode(rd, ix=None):
    op = rd.u8()

    if op in (0xDD, 0xFD):
        return decode(rd, 'IX' if op == 0xDD else 'IY')

    if op == 0xCB:
        if ix:
            d = rd.s8()
            op2 = rd.u8()
            tgt = f'({ix}{d:+d})'
            x, y, z = op2 >> 6, (op2 >> 3) & 7, op2 & 7
            extra = '' if z == 6 else f',{R[z]}'
            if x == 0:
                return f'{ROT[y]} {tgt}{extra}'
            if x == 1:
                return f'BIT {y},{tgt}'
            return f'{"RES" if x == 2 else "SET"} {y},{tgt}{extra}'
        op2 = rd.u8()
        x, y, z = op2 >> 6, (op2 >> 3) & 7, op2 & 7
        if x == 0:
            return f'{ROT[y]} {R[z]}'
        if x == 1:
            return f'BIT {y},{R[z]}'
        return f'{"RES" if x == 2 else "SET"} {y},{R[z]}'

    if op == 0xED:
        op2 = rd.u8()
        y, p, q = (op2 >> 3) & 7, (op2 >> 4) & 3, (op2 >> 3) & 1
        block = {0xA0: 'LDI', 0xA1: 'CPI', 0xA2: 'INI', 0xA3: 'OUTI',
                 0xA8: 'LDD', 0xA9: 'CPD', 0xAA: 'IND', 0xAB: 'OUTD',
                 0xB0: 'LDIR', 0xB1: 'CPIR', 0xB2: 'INIR', 0xB3: 'OTIR',
                 0xB8: 'LDDR', 0xB9: 'CPDR', 0xBA: 'INDR', 0xBB: 'OTDR'}
        if op2 in block:
            return block[op2]
        if (op2 & 0xC7) == 0x40:
            return f'IN {R[y] if y != 6 else "F"},(C)'
        if (op2 & 0xC7) == 0x41:
            return f'OUT (C),{R[y] if y != 6 else "0"}'
        if (op2 & 0xCF) == 0x42:
            return f'SBC HL,{RP[p]}'
        if (op2 & 0xCF) == 0x4A:
            return f'ADC HL,{RP[p]}'
        if (op2 & 0xCF) == 0x43:
            return f'LD (${rd.u16():04X}),{RP[p]}'
        if (op2 & 0xCF) == 0x4B:
            return f'LD {RP[p]},(${rd.u16():04X})'
        if (op2 & 0xC7) == 0x44:
            return 'NEG'
        if (op2 & 0xC7) == 0x45:
            return 'RETN' if op2 == 0x45 else 'RETI'
        if (op2 & 0xC7) == 0x46:
            return f'IM {[0, 0, 1, 2, 0, 0, 1, 2][y]}'
        return {0x47: 'LD I,A', 0x4F: 'LD R,A', 0x57: 'LD A,I',
                0x5F: 'LD A,R', 0x67: 'RRD', 0x6F: 'RLD'}.get(
                    op2, f'DEFB $ED,${op2:02X}')

    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1
    hl = _hl(ix)

    if x == 0:
        if z == 0:
            if y == 0:
                return 'NOP'
            if y == 1:
                return AF_[0]
            if y == 2:
                d = rd.s8()
                return f'DJNZ ${(rd.org + rd.p + d) & 0xFFFF:04X}'
            if y == 3:
                d = rd.s8()
                return f'JR ${(rd.org + rd.p + d) & 0xFFFF:04X}'
            d = rd.s8()
            return f'JR {CC[y-4]},${(rd.org + rd.p + d) & 0xFFFF:04X}'
        if z == 1:
            if q == 0:
                return f'LD {hl if p == 2 else RP[p]},${rd.u16():04X}'
            return f'ADD {hl},{hl if p == 2 else RP[p]}'
        if z == 2:
            if q == 0:
                if p == 0: return 'LD (BC),A'
                if p == 1: return 'LD (DE),A'
                if p == 2: return f'LD (${rd.u16():04X}),{hl}'
                return f'LD (${rd.u16():04X}),A'
            if p == 0: return 'LD A,(BC)'
            if p == 1: return 'LD A,(DE)'
            if p == 2: return f'LD {hl},(${rd.u16():04X})'
            return f'LD A,(${rd.u16():04X})'
        if z == 3:
            return f'{"INC" if q == 0 else "DEC"} {hl if p == 2 else RP[p]}'
        if z == 4:
            return f'INC {_r(y, ix, rd)}'
        if z == 5:
            return f'DEC {_r(y, ix, rd)}'
        if z == 6:
            t = _r(y, ix, rd)
            return f'LD {t},${rd.u8():02X}'
        return ['RLCA', 'RRCA', 'RLA', 'RRA', 'DAA', 'CPL', 'SCF', 'CCF'][y]

    if x == 1:
        if op == 0x76:
            return 'HALT'
        if z == 6:
            return f'LD {R[y]},{_r(6, ix, rd)}'
        if y == 6:
            return f'LD {_r(6, ix, rd)},{R[z]}'
        return f'LD {_r(y, ix, rd)},{_r(z, ix, rd)}'

    if x == 2:
        return f'{ALU[y]}{_r(z, ix, rd)}'

    if z == 0:
        return f'RET {CC[y]}'
    if z == 1:
        if q == 0:
            return f'POP {hl if p == 2 else RP2[p]}'
        return ['RET', 'EXX', f'JP ({hl})', f'LD SP,{hl}'][p]
    if z == 2:
        return f'JP {CC[y]},${rd.u16():04X}'
    if z == 3:
        if y == 0:
            return f'JP ${rd.u16():04X}'
        if y == 2:
            return f'OUT (${rd.u8():02X}),A'
        if y == 3:
            return f'IN A,(${rd.u8():02X})'
        return [None, None, None, None,
                f'EX (SP),{hl}', 'EX DE,HL', 'DI', 'EI'][y]
    if z == 4:
        return f'CALL {CC[y]},${rd.u16():04X}'
    if z == 5:
        if q == 0:
            return f'PUSH {hl if p == 2 else RP2[p]}'
        return f'CALL ${rd.u16():04X}'
    if z == 6:
        return f'{ALU[y]}${rd.u8():02X}'
    return f'RST ${y*8:02X}'


def disassemble(buf, org=0, start=0, count=64):
    rd = Reader(buf, start, org)
    out = []
    while count > 0 and rd.p < len(buf):
        at = rd.p
        try:
            text = decode(rd)
        except Exception:
            rd.p = at + 1
            text = f'DEFB ${buf[at]:02X}'
        raw = buf[at:rd.p]
        out.append(f'{org + at:04X}  {raw.hex().upper():<12} {text}')
        count -= 1
    return out


if __name__ == '__main__':
    import sys
    data = open(sys.argv[1], 'rb').read()
    org = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0
    start = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0
    n = int(sys.argv[4], 0) if len(sys.argv) > 4 else 64
    print('\n'.join(disassemble(data, org, start, n)))
