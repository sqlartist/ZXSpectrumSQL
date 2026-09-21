#!/usr/bin/env python3
"""
gen_z80.py -- emits a T-SQL Z80 interpreter for SQL Server.

Writing 700-odd opcodes by hand is a mug's game; most of the Z80 table is
highly regular, so we generate it. Output is a single stored procedure with a
balanced binary dispatch tree (8 comparisons per instruction rather than a
256-way IF chain).

Register file lives in scalar variables inside the proc. Memory is a table.
State is persisted to dbo.z80_state at proc entry/exit only.
"""

import sys

# ---------------------------------------------------------------- registers

R8 = {0: '@r_B', 1: '@r_C', 2: '@r_D', 3: '@r_E',
      4: '@r_H', 5: '@r_L', 7: '@r_A'}
RN = ['B', 'C', 'D', 'E', 'H', 'L', '(HL)', 'A']

HL = '(@r_H*256+@r_L)'
DE = '(@r_D*256+@r_E)'
BC = '(@r_B*256+@r_C)'

RP  = {0: BC, 1: DE, 2: HL, 3: '@r_SP'}
RPN = ['BC', 'DE', 'HL', 'SP']
RP2 = {0: BC, 1: DE, 2: HL, 3: '(@r_A*256+@r_F)'}
RP2N = ['BC', 'DE', 'HL', 'AF']

CC = ['NZ', 'Z', 'NC', 'C', 'PO', 'PE', 'P', 'M']
CCEXPR = [
    '(@r_F & 64) = 0',    # NZ
    '(@r_F & 64) <> 0',   # Z
    '(@r_F & 1) = 0',     # NC
    '(@r_F & 1) <> 0',    # C
    '(@r_F & 4) = 0',     # PO
    '(@r_F & 4) <> 0',    # PE
    '(@r_F & 128) = 0',   # P
    '(@r_F & 128) <> 0',  # M
]

L = []          # emitted lines for the current opcode
def w(s):
    L.append('    ' + s)

def flush():
    out = '\n'.join(L)
    L.clear()
    return out

# ------------------------------------------------------------ memory access

def rd(dst, addr, ts=3):
    """Read one byte of the 64K space into dst."""
    w(f'SET @ea = ({addr}) & 65535;')
    w(f'SELECT {dst} = val FROM dbo.z80_mem WHERE addr = @ea;')
    if ts:
        w(f'SET @ts = @ts + {ts};')

def wr(addr, val, ts=3):
    """Write one byte. Anything below 0x4000 is ROM and silently ignored."""
    w(f'SET @ea = ({addr}) & 65535;')
    w(f'IF @ea > 16383 UPDATE dbo.z80_mem SET val = ({val}) & 255 WHERE addr = @ea;')
    if ts:
        w(f'SET @ts = @ts + {ts};')

def fetch8(dst):
    """Fetch an immediate byte at PC and advance."""
    rd(dst, '@r_PC')
    w('SET @r_PC = (@r_PC + 1) & 65535;')

def fetch16(dst):
    fetch8('@n8')
    fetch8('@n8b')
    w(f'SET {dst} = @n8b*256 + @n8;')

def fetchd():
    """Fetch a signed displacement into @d."""
    fetch8('@n8')
    w('SET @d = CASE WHEN @n8 > 127 THEN @n8 - 256 ELSE @n8 END;')

def push(val):
    w('SET @r_SP = (@r_SP - 1) & 65535;')
    wr('@r_SP', f'(({val}) / 256) & 255')
    w('SET @r_SP = (@r_SP - 1) & 65535;')
    wr('@r_SP', f'({val}) & 255')

def pop(hi, lo):
    rd(lo, '@r_SP')
    w('SET @r_SP = (@r_SP + 1) & 65535;')
    rd(hi, '@r_SP')
    w('SET @r_SP = (@r_SP + 1) & 65535;')

def hl_ea(dst='@ea2'):
    """
    Effective address for the (HL) operand.

    Under a DD/FD prefix H/L have already been swapped with IXh/IXl, so the
    index register is sitting in @r_H/@r_L either way -- we only need to pick
    up the displacement byte.
    """
    w(f'SET {dst} = @r_H*256 + @r_L;')
    w('IF @ixy <> 0')
    w('BEGIN')
    fetchd()
    w(f'    SET {dst} = ({dst} + @d) & 65535;')
    w('    SET @ts = @ts + 5;')
    w('END;')

def real_r(i):
    """
    Read a register, undoing the DD/FD swap for H and L. Used by the
    LD r,(IX+d) / LD (IX+d),r forms where the register operand stays H or L.
    """
    if i == 4:
        return '(CASE WHEN @ixy = 0 THEN @r_H WHEN @ixy = 1 THEN @r_IXH ELSE @r_IYH END)'
    if i == 5:
        return '(CASE WHEN @ixy = 0 THEN @r_L WHEN @ixy = 1 THEN @r_IXL ELSE @r_IYL END)'
    return R8[i]

def set_real_r(i, expr):
    if i in (4, 5):
        hl = '@r_H' if i == 4 else '@r_L'
        ix = '@r_IXH' if i == 4 else '@r_IXL'
        iy = '@r_IYH' if i == 4 else '@r_IYL'
        w(f'IF @ixy = 0')
        w(f'BEGIN SET {hl} = ({expr}) & 255; END')
        w(f'ELSE IF @ixy = 1')
        w(f'BEGIN SET {ix} = ({expr}) & 255; END')
        w(f'ELSE')
        w(f'BEGIN SET {iy} = ({expr}) & 255; END')
    else:
        w(f'SET {R8[i]} = ({expr}) & 255;')

# ------------------------------------------------------------------- flags
# bit 7 S, 6 Z, 5 F5, 4 H, 3 F3, 2 P/V, 1 N, 0 C

def f_szp(src):
    """S Z 5 3 P from a lookup table, N and H cleared."""
    w(f'SELECT @r_F = f FROM dbo.z80_szp WHERE v = ({src}) & 255;')

def alu_add(src, carry=False):
    c = ' + (@r_F & 1)' if carry else ''
    w(f'SET @v = ({src}) & 255;')
    w(f'SET @t = @r_A + @v{c};')
    w('SET @r_F = (CASE WHEN @t > 255 THEN 1 ELSE 0 END)')
    w('         | (CASE WHEN ((@r_A ^ @v ^ @t) & 16) <> 0 THEN 16 ELSE 0 END)')
    w('         | (CASE WHEN ((@r_A ^ @t) & (@v ^ @t) & 128) <> 0 THEN 4 ELSE 0 END);')
    w('SET @r_A = @t & 255;')
    w('SET @r_F = @r_F | (CASE WHEN @r_A = 0 THEN 64 ELSE 0 END) | (@r_A & 168);')

def alu_sub(src, carry=False, cp=False):
    c = ' - (@r_F & 1)' if carry else ''
    w(f'SET @v = ({src}) & 255;')
    w(f'SET @t = @r_A - @v{c};')
    w('SET @res = @t & 255;')
    w('SET @r_F = 2 | (CASE WHEN @t < 0 THEN 1 ELSE 0 END)')
    w('         | (CASE WHEN ((@r_A ^ @v ^ @t) & 16) <> 0 THEN 16 ELSE 0 END)')
    w('         | (CASE WHEN ((@r_A ^ @v) & (@r_A ^ @res) & 128) <> 0 THEN 4 ELSE 0 END)')
    w('         | (CASE WHEN @res = 0 THEN 64 ELSE 0 END) | (@res & 128);')
    if cp:
        # undocumented 5/3 come from the operand on CP
        w('SET @r_F = @r_F | (@v & 40);')
    else:
        w('SET @r_F = @r_F | (@res & 40);')
        w('SET @r_A = @res;')

def alu_and(src):
    w(f'SET @r_A = @r_A & (({src}) & 255);')
    f_szp('@r_A')
    w('SET @r_F = @r_F | 16;')

def alu_xor(src):
    w(f'SET @r_A = @r_A ^ (({src}) & 255);')
    f_szp('@r_A')

def alu_or(src):
    w(f'SET @r_A = @r_A | (({src}) & 255);')
    f_szp('@r_A')

def alu(op, src):
    [alu_add, lambda s: alu_add(s, True),
     alu_sub, lambda s: alu_sub(s, True),
     alu_and, alu_xor, alu_or,
     lambda s: alu_sub(s, False, True)][op](src)

ALUN = ['ADD A,', 'ADC A,', 'SUB ', 'SBC A,', 'AND ', 'XOR ', 'OR ', 'CP ']

def inc8(src, dst_setter):
    w(f'SET @res = (({src}) + 1) & 255;')
    w('SET @r_F = (@r_F & 1) | (CASE WHEN @res = 0 THEN 64 ELSE 0 END) | (@res & 168)')
    w('         | (CASE WHEN (@res & 15) = 0 THEN 16 ELSE 0 END)')
    w('         | (CASE WHEN @res = 128 THEN 4 ELSE 0 END);')
    dst_setter('@res')

def dec8(src, dst_setter):
    w(f'SET @res = (({src}) - 1) & 255;')
    w('SET @r_F = (@r_F & 1) | 2 | (CASE WHEN @res = 0 THEN 64 ELSE 0 END) | (@res & 168)')
    w('         | (CASE WHEN (@res & 15) = 15 THEN 16 ELSE 0 END)')
    w('         | (CASE WHEN @res = 127 THEN 4 ELSE 0 END);')
    dst_setter('@res')

def add16(dst_hi, dst_lo, src):
    w(f'SET @v = {src};')
    w(f'SET @nn = {dst_hi}*256 + {dst_lo};')
    w('SET @t = @nn + @v;')
    w('SET @r_F = (@r_F & 196) | (CASE WHEN @t > 65535 THEN 1 ELSE 0 END)')
    w('         | (CASE WHEN ((@nn ^ @v ^ @t) & 4096) <> 0 THEN 16 ELSE 0 END)')
    w('         | ((@t / 256) & 40);')
    w(f'SET {dst_hi} = (@t / 256) & 255;')
    w(f'SET {dst_lo} = @t & 255;')
    w('SET @ts = @ts + 7;')

# --------------------------------------------------------- operand helpers

def get_r(i, dst='@v', ea_done=False):
    """Read register i. i == 6 means (HL) / (IX+d) and emits the address calc."""
    if i == 6:
        if not ea_done:
            hl_ea()
        rd(dst, '@ea2')
        return dst
    return R8[i]

def put_r(i, expr):
    if i == 6:
        wr('@ea2', expr)
    else:
        w(f'SET {R8[i]} = ({expr}) & 255;')

def in_port(dst, port):
    w(f'SET @nn = ({port}) & 65535;')
    w('SET @res = 255;')
    w('IF (@nn & 32) = 0')
    w('BEGIN SET @res = @kempston; END')
    w('ELSE IF (@nn & 1) = 0')
    w('BEGIN')
    w('    SELECT @res = ISNULL(MIN(val), 255) FROM dbo.z80_kb WHERE ((@nn / 256) & mask) = 0;')
    w('    SET @res = (@res & 31) | 224;')
    w('END')
    w(f'SET {dst} = @res;')
    w('SET @ts = @ts + 4;')

def out_port(port, val):
    w(f'SET @nn = ({port}) & 65535;')
    w(f'SET @v = ({val}) & 255;')
    w('IF (@nn & 1) = 0 SET @border = @v & 7;')
    w('SET @ts = @ts + 4;')

# ============================================================ BASE TABLE

def gen_base(op):
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1

    # ---- x = 0 -------------------------------------------------------
    if x == 0:
        if z == 0:
            if y == 0:
                w('-- NOP')
            elif y == 1:
                w('-- EX AF,AF\'')
                w('SET @t = @r_A; SET @r_A = @r_A2; SET @r_A2 = @t;')
                w('SET @t = @r_F; SET @r_F = @r_F2; SET @r_F2 = @t;')
            elif y == 2:
                w('-- DJNZ d')
                fetchd()
                w('SET @r_B = (@r_B - 1) & 255;')
                w('IF @r_B <> 0 BEGIN SET @r_PC = (@r_PC + @d) & 65535; SET @ts = @ts + 5; END;')
                w('SET @ts = @ts + 1;')
            elif y == 3:
                w('-- JR d')
                fetchd()
                w('SET @r_PC = (@r_PC + @d) & 65535;')
                w('SET @ts = @ts + 5;')
            else:
                cc = y - 4
                w(f'-- JR {CC[cc]},d')
                fetchd()
                w(f'IF {CCEXPR[cc]} BEGIN SET @r_PC = (@r_PC + @d) & 65535; SET @ts = @ts + 5; END;')
        elif z == 1:
            if q == 0:
                w(f'-- LD {RPN[p]},nn')
                fetch16('@nn')
                if p == 3:
                    w('SET @r_SP = @nn;')
                else:
                    hi, lo = [('@r_B', '@r_C'), ('@r_D', '@r_E'), ('@r_H', '@r_L')][p]
                    w(f'SET {hi} = (@nn / 256) & 255; SET {lo} = @nn & 255;')
            else:
                w(f'-- ADD HL,{RPN[p]}')
                add16('@r_H', '@r_L', RP[p])
        elif z == 2:
            if q == 0:
                if p == 0:
                    w('-- LD (BC),A'); wr(BC, '@r_A')
                elif p == 1:
                    w('-- LD (DE),A'); wr(DE, '@r_A')
                elif p == 2:
                    w('-- LD (nn),HL'); fetch16('@nn')
                    wr('@nn', '@r_L'); wr('(@nn + 1)', '@r_H')
                else:
                    w('-- LD (nn),A'); fetch16('@nn'); wr('@nn', '@r_A')
            else:
                if p == 0:
                    w('-- LD A,(BC)'); rd('@r_A', BC)
                elif p == 1:
                    w('-- LD A,(DE)'); rd('@r_A', DE)
                elif p == 2:
                    w('-- LD HL,(nn)'); fetch16('@nn')
                    rd('@r_L', '@nn'); rd('@r_H', '(@nn + 1)')
                else:
                    w('-- LD A,(nn)'); fetch16('@nn'); rd('@r_A', '@nn')
        elif z == 3:
            delta = 1 if q == 0 else -1
            w(f'-- {"INC" if q == 0 else "DEC"} {RPN[p]}')
            if p == 3:
                w(f'SET @r_SP = (@r_SP + ({delta})) & 65535;')
            else:
                hi, lo = [('@r_B', '@r_C'), ('@r_D', '@r_E'), ('@r_H', '@r_L')][p]
                w(f'SET @nn = ({hi}*256 + {lo} + ({delta})) & 65535;')
                w(f'SET {hi} = (@nn / 256) & 255; SET {lo} = @nn & 255;')
            w('SET @ts = @ts + 2;')
        elif z == 4:
            w(f'-- INC {RN[y]}')
            src = get_r(y)
            inc8(src, lambda e: put_r(y, e))
            if y == 6:
                w('SET @ts = @ts + 1;')
        elif z == 5:
            w(f'-- DEC {RN[y]}')
            src = get_r(y)
            dec8(src, lambda e: put_r(y, e))
            if y == 6:
                w('SET @ts = @ts + 1;')
        elif z == 6:
            w(f'-- LD {RN[y]},n')
            if y == 6:
                hl_ea()
                fetch8('@n8')
                wr('@ea2', '@n8')
            else:
                fetch8('@n8')
                w(f'SET {R8[y]} = @n8;')
        else:
            if y == 0:
                w('-- RLCA')
                w('SET @c = (@r_A / 128) & 1;')
                w('SET @r_A = ((@r_A * 2) & 255) | @c;')
                w('SET @r_F = (@r_F & 196) | @c | (@r_A & 40);')
            elif y == 1:
                w('-- RRCA')
                w('SET @c = @r_A & 1;')
                w('SET @r_A = (@r_A / 2) | (@c * 128);')
                w('SET @r_F = (@r_F & 196) | @c | (@r_A & 40);')
            elif y == 2:
                w('-- RLA')
                w('SET @c = (@r_A / 128) & 1;')
                w('SET @r_A = ((@r_A * 2) & 255) | (@r_F & 1);')
                w('SET @r_F = (@r_F & 196) | @c | (@r_A & 40);')
            elif y == 3:
                w('-- RRA')
                w('SET @c = @r_A & 1;')
                w('SET @r_A = (@r_A / 2) | ((@r_F & 1) * 128);')
                w('SET @r_F = (@r_F & 196) | @c | (@r_A & 40);')
            elif y == 4:
                w('-- DAA')
                w('SET @v = 0; SET @c = 0;')
                w('IF (@r_F & 16) <> 0 OR (@r_A & 15) > 9 SET @v = 6;')
                w('IF (@r_F & 1) <> 0 OR @r_A > 153 BEGIN SET @v = @v + 96; SET @c = 1; END;')
                w('IF (@r_F & 2) <> 0')
                w('BEGIN')
                w('    SET @t = (@r_A - @v) & 255;')
                w('    SET @res = CASE WHEN (@r_F & 16) <> 0 AND (@r_A & 15) < 6 THEN 16 ELSE 0 END;')
                w('END')
                w('ELSE')
                w('BEGIN')
                w('    SET @t = (@r_A + @v) & 255;')
                w('    SET @res = CASE WHEN (@r_A & 15) > 9 THEN 16 ELSE 0 END;')
                w('END;')
                w('SET @r_A = @t;')
                w('SELECT @nn = f FROM dbo.z80_szp WHERE v = @r_A;')
                w('SET @r_F = (@r_F & 2) | @c | @res | (@nn & 236);')
            elif y == 5:
                w('-- CPL')
                w('SET @r_A = @r_A ^ 255;')
                w('SET @r_F = (@r_F & 197) | 18 | (@r_A & 40);')
            elif y == 6:
                w('-- SCF')
                w('SET @r_F = (@r_F & 196) | 1 | (@r_A & 40);')
            else:
                w('-- CCF')
                w('SET @c = @r_F & 1;')
                w('SET @r_F = (@r_F & 196) | (@c * 16) | (1 - @c) | (@r_A & 40);')

    # ---- x = 1 : LD r,r' + HALT ---------------------------------------
    elif x == 1:
        if op == 0x76:
            w('-- HALT')
            w('SET @halt = 1;')
            w('SET @r_PC = (@r_PC - 1) & 65535;')
        elif z == 6:
            w(f'-- LD {RN[y]},(HL)')
            hl_ea()
            rd('@v', '@ea2')
            set_real_r(y, '@v')
        elif y == 6:
            w(f'-- LD (HL),{RN[z]}')
            hl_ea()
            wr('@ea2', real_r(z))
        else:
            w(f'-- LD {RN[y]},{RN[z]}')
            w(f'SET {R8[y]} = {R8[z]};')

    # ---- x = 2 : ALU A,r ----------------------------------------------
    elif x == 2:
        w(f'-- {ALUN[y]}{RN[z]}')
        src = get_r(z)
        alu(y, src)

    # ---- x = 3 ---------------------------------------------------------
    else:
        if z == 0:
            w(f'-- RET {CC[y]}')
            w(f'IF {CCEXPR[y]}')
            w('BEGIN')
            pop('@n8b', '@n8')
            w('    SET @r_PC = @n8b*256 + @n8;')
            w('END;')
            w('SET @ts = @ts + 1;')
        elif z == 1:
            if q == 0:
                w(f'-- POP {RP2N[p]}')
                hi, lo = [('@r_B', '@r_C'), ('@r_D', '@r_E'),
                          ('@r_H', '@r_L'), ('@r_A', '@r_F')][p]
                pop(hi, lo)
            else:
                if p == 0:
                    w('-- RET')
                    pop('@n8b', '@n8')
                    w('SET @r_PC = @n8b*256 + @n8;')
                elif p == 1:
                    w('-- EXX')
                    for a, b in [('@r_B', '@r_B2'), ('@r_C', '@r_C2'), ('@r_D', '@r_D2'),
                                 ('@r_E', '@r_E2'), ('@r_H', '@r_H2'), ('@r_L', '@r_L2')]:
                        w(f'SET @t = {a}; SET {a} = {b}; SET {b} = @t;')
                elif p == 2:
                    w('-- JP (HL)')
                    w(f'SET @r_PC = {HL};')
                else:
                    w('-- LD SP,HL')
                    w(f'SET @r_SP = {HL};')
                    w('SET @ts = @ts + 2;')
        elif z == 2:
            w(f'-- JP {CC[y]},nn')
            fetch16('@nn')
            w(f'IF {CCEXPR[y]} SET @r_PC = @nn;')
        elif z == 3:
            if y == 0:
                w('-- JP nn')
                fetch16('@nn')
                w('SET @r_PC = @nn;')
            elif y == 1:
                w('-- CB prefix handled by caller')
            elif y == 2:
                w('-- OUT (n),A')
                fetch8('@n8')
                out_port('(@r_A*256 + @n8)', '@r_A')
            elif y == 3:
                w('-- IN A,(n)')
                fetch8('@n8')
                in_port('@r_A', '(@r_A*256 + @n8)')
            elif y == 4:
                w('-- EX (SP),HL')
                rd('@n8', '@r_SP')
                rd('@n8b', '(@r_SP + 1)')
                wr('@r_SP', '@r_L')
                wr('(@r_SP + 1)', '@r_H')
                w('SET @r_L = @n8; SET @r_H = @n8b;')
                w('SET @ts = @ts + 3;')
            elif y == 5:
                w('-- EX DE,HL  (unaffected by DD/FD)')
                w('IF @ixy = 0')
                w('BEGIN')
                w('    SET @t = @r_D; SET @r_D = @r_H; SET @r_H = @t;')
                w('    SET @t = @r_E; SET @r_E = @r_L; SET @r_L = @t;')
                w('END')
                w('ELSE IF @ixy = 1')
                w('BEGIN')
                w('    SET @t = @r_D; SET @r_D = @r_IXH; SET @r_IXH = @t;')
                w('    SET @t = @r_E; SET @r_E = @r_IXL; SET @r_IXL = @t;')
                w('END')
                w('ELSE')
                w('BEGIN')
                w('    SET @t = @r_D; SET @r_D = @r_IYH; SET @r_IYH = @t;')
                w('    SET @t = @r_E; SET @r_E = @r_IYL; SET @r_IYL = @t;')
                w('END;')
            elif y == 6:
                w('-- DI')
                w('SET @iff1 = 0; SET @iff2 = 0;')
            else:
                w('-- EI')
                w('SET @iff1 = 1; SET @iff2 = 1; SET @ei_pending = 1;')
        elif z == 4:
            w(f'-- CALL {CC[y]},nn')
            fetch16('@nn')
            w(f'IF {CCEXPR[y]}')
            w('BEGIN')
            push('@r_PC')
            w('    SET @r_PC = @nn;')
            w('    SET @ts = @ts + 1;')
            w('END;')
        elif z == 5:
            if q == 0:
                w(f'-- PUSH {RP2N[p]}')
                push(RP2[p])
                w('SET @ts = @ts + 1;')
            else:
                if p == 0:
                    w('-- CALL nn')
                    fetch16('@nn')
                    push('@r_PC')
                    w('SET @r_PC = @nn;')
                    w('SET @ts = @ts + 1;')
                else:
                    w('-- prefix handled by caller')
        elif z == 6:
            w(f'-- {ALUN[y]}n')
            fetch8('@n8')
            alu(y, '@n8')
        else:
            w(f'-- RST {y*8:02X}h')
            push('@r_PC')
            w(f'SET @r_PC = {y*8};')
            w('SET @ts = @ts + 1;')

    return flush()

# ============================================================ CB TABLE
# CB is fully regular, so decode the operand once at runtime rather than
# emitting 256 near-identical blocks.

ROTN = ['RLC', 'RRC', 'RL', 'RR', 'SLA', 'SRA', 'SLL', 'SRL']

def gen_cb_ops():
    o = []
    def e(s): o.append('        ' + s)
    e('-- rotates / shifts')
    for y in range(8):
        kw = 'IF' if y == 0 else 'ELSE IF'
        e(f'{kw} @cby = {y}   -- {ROTN[y]}')
        e('BEGIN')
        if y == 0:
            e('    SET @c = (@v / 128) & 1; SET @res = ((@v * 2) & 255) | @c;')
        elif y == 1:
            e('    SET @c = @v & 1; SET @res = (@v / 2) | (@c * 128);')
        elif y == 2:
            e('    SET @c = (@v / 128) & 1; SET @res = ((@v * 2) & 255) | (@r_F & 1);')
        elif y == 3:
            e('    SET @c = @v & 1; SET @res = (@v / 2) | ((@r_F & 1) * 128);')
        elif y == 4:
            e('    SET @c = (@v / 128) & 1; SET @res = (@v * 2) & 255;')
        elif y == 5:
            e('    SET @c = @v & 1; SET @res = (@v & 128) | (@v / 2);')
        elif y == 6:
            e('    SET @c = (@v / 128) & 1; SET @res = ((@v * 2) & 255) | 1;')
        else:
            e('    SET @c = @v & 1; SET @res = @v / 2;')
        e('    SELECT @r_F = f FROM dbo.z80_szp WHERE v = @res;')
        e('    SET @r_F = @r_F | @c;')
        e('END')
    return '\n'.join(o)

# ============================================================ ED TABLE

def gen_ed(op):
    y = (op >> 3) & 7
    p, q = y >> 1, y & 1

    if op in (0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70, 0x78):
        w(f'-- IN {RN[y] if y != 6 else "F"},(C)')
        in_port('@v', BC)
        if y != 6:
            w(f'SET {R8[y]} = @v;')
        w('SELECT @r_F = f FROM dbo.z80_szp WHERE v = @v;')
        w('SET @r_F = @r_F | (@r_F_old & 1);')
    elif op in (0x41, 0x49, 0x51, 0x59, 0x61, 0x69, 0x71, 0x79):
        w(f'-- OUT (C),{RN[y] if y != 6 else "0"}')
        out_port(BC, R8[y] if y != 6 else '0')
    elif op in (0x42, 0x52, 0x62, 0x72):
        w(f'-- SBC HL,{RPN[p]}')
        w(f'SET @v = {RP[p]};')
        w(f'SET @nn = {HL};')
        w('SET @t = @nn - @v - (@r_F & 1);')
        w('SET @res = @t & 65535;')
        w('SET @r_F = 2 | (CASE WHEN @t < 0 THEN 1 ELSE 0 END)')
        w('         | (CASE WHEN ((@nn ^ @v ^ @t) & 4096) <> 0 THEN 16 ELSE 0 END)')
        w('         | (CASE WHEN ((@nn ^ @v) & (@nn ^ @res) & 32768) <> 0 THEN 4 ELSE 0 END)')
        w('         | (CASE WHEN @res = 0 THEN 64 ELSE 0 END) | ((@res / 256) & 168);')
        w('SET @r_H = (@res / 256) & 255; SET @r_L = @res & 255;')
        w('SET @ts = @ts + 7;')
    elif op in (0x4A, 0x5A, 0x6A, 0x7A):
        w(f'-- ADC HL,{RPN[p]}')
        w(f'SET @v = {RP[p]};')
        w(f'SET @nn = {HL};')
        w('SET @t = @nn + @v + (@r_F & 1);')
        w('SET @res = @t & 65535;')
        w('SET @r_F = (CASE WHEN @t > 65535 THEN 1 ELSE 0 END)')
        w('         | (CASE WHEN ((@nn ^ @v ^ @t) & 4096) <> 0 THEN 16 ELSE 0 END)')
        w('         | (CASE WHEN ((@nn ^ @t) & (@v ^ @t) & 32768) <> 0 THEN 4 ELSE 0 END)')
        w('         | (CASE WHEN @res = 0 THEN 64 ELSE 0 END) | ((@res / 256) & 168);')
        w('SET @r_H = (@res / 256) & 255; SET @r_L = @res & 255;')
        w('SET @ts = @ts + 7;')
    elif op in (0x43, 0x53, 0x63, 0x73):
        w(f'-- LD (nn),{RPN[p]}')
        fetch16('@nn')
        w(f'SET @v = {RP[p]};')
        wr('@nn', '@v & 255')
        wr('(@nn + 1)', '(@v / 256) & 255')
    elif op in (0x4B, 0x5B, 0x6B, 0x7B):
        w(f'-- LD {RPN[p]},(nn)')
        fetch16('@nn')
        rd('@n8', '@nn')
        rd('@n8b', '(@nn + 1)')
        if p == 3:
            w('SET @r_SP = @n8b*256 + @n8;')
        else:
            hi, lo = [('@r_B', '@r_C'), ('@r_D', '@r_E'), ('@r_H', '@r_L')][p]
            w(f'SET {hi} = @n8b; SET {lo} = @n8;')
    elif (op & 0xC7) == 0x44:
        w('-- NEG')
        w('SET @v = @r_A; SET @r_A = 0;')
        alu_sub('@v')
    elif (op & 0xC7) == 0x45:
        w('-- RETN / RETI')
        pop('@n8b', '@n8')
        w('SET @r_PC = @n8b*256 + @n8;')
        w('SET @iff1 = @iff2;')
    elif (op & 0xC7) == 0x46:
        m = {0: 0, 1: 0, 2: 1, 3: 2, 4: 0, 5: 0, 6: 1, 7: 2}[y]
        w(f'-- IM {m}')
        w(f'SET @im = {m};')
    elif op == 0x47:
        w('-- LD I,A'); w('SET @r_I = @r_A; SET @ts = @ts + 1;')
    elif op == 0x4F:
        w('-- LD R,A'); w('SET @r_R = @r_A; SET @ts = @ts + 1;')
    elif op == 0x57:
        w('-- LD A,I')
        w('SET @r_A = @r_I;')
        w('SELECT @r_F = f FROM dbo.z80_szp WHERE v = @r_A;')
        w('SET @r_F = (@r_F & 168) | (CASE WHEN @r_A = 0 THEN 64 ELSE 0 END)')
        w('         | (CASE WHEN @iff2 = 1 THEN 4 ELSE 0 END) | (@r_F_old & 1);')
        w('SET @ts = @ts + 1;')
    elif op == 0x5F:
        w('-- LD A,R')
        w('SET @r_A = @r_R;')
        w('SET @r_F = (@r_A & 168) | (CASE WHEN @r_A = 0 THEN 64 ELSE 0 END)')
        w('         | (CASE WHEN @iff2 = 1 THEN 4 ELSE 0 END) | (@r_F_old & 1);')
        w('SET @ts = @ts + 1;')
    elif op == 0x67:
        w('-- RRD')
        rd('@v', HL)
        w('SET @t = @r_A;')
        w('SET @r_A = (@t & 240) | (@v & 15);')
        wr(HL, '((@v / 16) & 15) | ((@t & 15) * 16)')
        w('SELECT @r_F = f FROM dbo.z80_szp WHERE v = @r_A;')
        w('SET @r_F = @r_F | (@r_F_old & 1);')
        w('SET @ts = @ts + 4;')
    elif op == 0x6F:
        w('-- RLD')
        rd('@v', HL)
        w('SET @t = @r_A;')
        w('SET @r_A = (@t & 240) | ((@v / 16) & 15);')
        wr(HL, '(((@v & 15) * 16) | (@t & 15))')
        w('SELECT @r_F = f FROM dbo.z80_szp WHERE v = @r_A;')
        w('SET @r_F = @r_F | (@r_F_old & 1);')
        w('SET @ts = @ts + 4;')
    elif op in (0xA0, 0xA8, 0xB0, 0xB8):
        rep = op >= 0xB0
        step = 1 if op in (0xA0, 0xB0) else -1
        w(f'-- {"LDIR" if op == 0xB0 else "LDDR" if op == 0xB8 else "LDI" if op == 0xA0 else "LDD"}')
        rd('@v', HL)
        wr(DE, '@v')
        w(f'SET @nn = ({DE} + ({step})) & 65535;')
        w('SET @r_D = (@nn / 256) & 255; SET @r_E = @nn & 255;')
        w(f'SET @nn = ({HL} + ({step})) & 65535;')
        w('SET @r_H = (@nn / 256) & 255; SET @r_L = @nn & 255;')
        w(f'SET @nn = ({BC} - 1) & 65535;')
        w('SET @r_B = (@nn / 256) & 255; SET @r_C = @nn & 255;')
        w('SET @t = (@r_A + @v) & 255;')
        w('SET @r_F = (@r_F & 193) | (CASE WHEN @nn <> 0 THEN 4 ELSE 0 END)')
        w('         | (CASE WHEN (@t & 2) <> 0 THEN 32 ELSE 0 END)')
        w('         | (CASE WHEN (@t & 8) <> 0 THEN 8 ELSE 0 END);')
        w('SET @ts = @ts + 2;')
        if rep:
            w('IF @nn <> 0 BEGIN SET @r_PC = (@r_PC - 2) & 65535; SET @ts = @ts + 5; END;')
    elif op in (0xA1, 0xA9, 0xB1, 0xB9):
        rep = op >= 0xB0
        step = 1 if op in (0xA1, 0xB1) else -1
        w(f'-- {"CPIR" if op == 0xB1 else "CPDR" if op == 0xB9 else "CPI" if op == 0xA1 else "CPD"}')
        rd('@v', HL)
        w('SET @t = (@r_A - @v) & 255;')
        w(f'SET @nn = ({HL} + ({step})) & 65535;')
        w('SET @r_H = (@nn / 256) & 255; SET @r_L = @nn & 255;')
        w(f'SET @nn = ({BC} - 1) & 65535;')
        w('SET @r_B = (@nn / 256) & 255; SET @r_C = @nn & 255;')
        w('SET @c = CASE WHEN ((@r_A ^ @v ^ @t) & 16) <> 0 THEN 16 ELSE 0 END;')
        w('SET @res = (@t - (CASE WHEN @c <> 0 THEN 1 ELSE 0 END)) & 255;')
        w('SET @r_F = (@r_F & 1) | 2 | @c | (@t & 128)')
        w('         | (CASE WHEN @t = 0 THEN 64 ELSE 0 END)')
        w('         | (CASE WHEN @nn <> 0 THEN 4 ELSE 0 END)')
        w('         | (CASE WHEN (@res & 2) <> 0 THEN 32 ELSE 0 END)')
        w('         | (CASE WHEN (@res & 8) <> 0 THEN 8 ELSE 0 END);')
        w('SET @ts = @ts + 5;')
        if rep:
            w('IF @nn <> 0 AND @t <> 0 BEGIN SET @r_PC = (@r_PC - 2) & 65535; SET @ts = @ts + 5; END;')
    else:
        w('-- ED NOP (unimplemented / undocumented)')

    return flush()

# ============================================================ DISPATCH TREE

def _has_statement(body):
    """True if the block contains anything other than comments."""
    if not body:
        return False
    return any(ln.strip() and not ln.strip().startswith('--')
               for ln in body.splitlines())


def tree(codes, lo, hi, indent):
    """Balanced binary search over opcode value."""
    pad = '    ' * indent
    if hi - lo == 1:
        body = codes.get(lo)
        if not _has_statement(body):
            # A comment-only block is a syntax error in T-SQL, so give the
            # no-op opcodes an actual no-op.
            note = body.strip() if body else '-- undefined opcode'
            body = f'    {note}\n    SET @v = @v;'
        return f'{pad}-- opcode {lo:02X}h\n{pad}BEGIN\n{body}\n{pad}END\n'
    mid = (lo + hi) // 2
    out = f'{pad}IF @op < {mid}\n{pad}BEGIN\n'
    out += tree(codes, lo, mid, indent + 1)
    out += f'{pad}END\n{pad}ELSE\n{pad}BEGIN\n'
    out += tree(codes, mid, hi, indent + 1)
    out += f'{pad}END\n'
    return out

# ============================================================ PROC WRAPPER

HEADER = r"""/* =====================================================================
   02_z80_core.sql  --  GENERATED BY gen_z80.py.  DO NOT EDIT BY HAND.

   A Z80 interpreter as a single T-SQL stored procedure.

   Registers live in scalar variables for the duration of the call and are
   flushed to dbo.z80_state on exit, so a call should cover at least a whole
   frame's worth of T-states to amortise the load/store.

   Memory is dbo.z80_mem (addr, val). Writes below 0x4000 are dropped: that
   is the ROM.
   ===================================================================== */
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

CREATE OR ALTER PROCEDURE dbo.z80_run
    @budget      int  = 69888,   -- T-states to execute (one 48K frame)
    @assert_int  bit  = 1,       -- raise the 50Hz maskable interrupt first
    @stop_pc     int  = NULL,    -- breakpoint: halt the moment PC reaches this
    @out_ts      int  = NULL OUTPUT,
    @out_instr   int  = NULL OUTPUT,
    @out_stopped bit  = NULL OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @r_A tinyint, @r_F tinyint, @r_B tinyint, @r_C tinyint,
            @r_D tinyint, @r_E tinyint, @r_H tinyint, @r_L tinyint,
            @r_A2 tinyint, @r_F2 tinyint, @r_B2 tinyint, @r_C2 tinyint,
            @r_D2 tinyint, @r_E2 tinyint, @r_H2 tinyint, @r_L2 tinyint,
            @r_IXH tinyint, @r_IXL tinyint, @r_IYH tinyint, @r_IYL tinyint,
            @r_I tinyint, @r_R tinyint,
            @r_SP int, @r_PC int,
            @iff1 tinyint, @iff2 tinyint, @im tinyint, @halt tinyint,
            @ei_pending tinyint, @border tinyint, @kempston tinyint;

    DECLARE @op tinyint, @n8 tinyint, @n8b tinyint, @ixy tinyint,
            @r_F_old tinyint, @cbx tinyint, @cby tinyint, @cbz tinyint,
            @bm int, @memop tinyint;

    DECLARE @nn int, @ea int, @ea2 int, @t int, @v int, @d int,
            @c int, @res int, @ts int = 0, @instr int = 0, @stopped bit = 0;

    SELECT @r_A = r_A, @r_F = r_F, @r_B = r_B, @r_C = r_C,
           @r_D = r_D, @r_E = r_E, @r_H = r_H, @r_L = r_L,
           @r_A2 = r_A2, @r_F2 = r_F2, @r_B2 = r_B2, @r_C2 = r_C2,
           @r_D2 = r_D2, @r_E2 = r_E2, @r_H2 = r_H2, @r_L2 = r_L2,
           @r_IXH = r_IXH, @r_IXL = r_IXL, @r_IYH = r_IYH, @r_IYL = r_IYL,
           @r_I = r_I, @r_R = r_R, @r_SP = r_SP, @r_PC = r_PC,
           @iff1 = iff1, @iff2 = iff2, @im = im, @halt = halt,
           @ei_pending = ei_pending, @border = border, @kempston = kempston
    FROM dbo.z80_state WHERE id = 1;

    /* ---- maskable interrupt ------------------------------------- */
    IF @assert_int = 1 AND @iff1 = 1 AND @ei_pending = 0
    BEGIN
        IF @halt = 1 BEGIN SET @halt = 0; SET @r_PC = (@r_PC + 1) & 65535; END;
        SET @iff1 = 0; SET @iff2 = 0;
        SET @r_SP = (@r_SP - 1) & 65535;
        SET @ea = @r_SP;
        IF @ea > 16383 UPDATE dbo.z80_mem SET val = (@r_PC / 256) & 255 WHERE addr = @ea;
        SET @r_SP = (@r_SP - 1) & 65535;
        SET @ea = @r_SP;
        IF @ea > 16383 UPDATE dbo.z80_mem SET val = @r_PC & 255 WHERE addr = @ea;
        IF @im = 2
        BEGIN
            SET @ea = (@r_I * 256 + 255) & 65535;
            SELECT @n8 = val FROM dbo.z80_mem WHERE addr = @ea;
            SET @ea = (@ea + 1) & 65535;
            SELECT @n8b = val FROM dbo.z80_mem WHERE addr = @ea;
            SET @r_PC = @n8b * 256 + @n8;
        END
        ELSE SET @r_PC = 56;
        SET @ts = @ts + 19;
    END;

    /* ---- main fetch/execute loop -------------------------------- */
    WHILE @ts < @budget
    BEGIN
        SET @ixy = 0;

        SET @ea = @r_PC;
        SELECT @op = val FROM dbo.z80_mem WHERE addr = @ea;
        SET @r_PC = (@r_PC + 1) & 65535;
        SET @r_R = (@r_R & 128) | ((@r_R + 1) & 127);
        SET @ts = @ts + 4;
        SET @instr = @instr + 1;

        WHILE @op = 221 OR @op = 253
        BEGIN
            SET @ixy = CASE WHEN @op = 221 THEN 1 ELSE 2 END;
            SET @ea = @r_PC;
            SELECT @op = val FROM dbo.z80_mem WHERE addr = @ea;
            SET @r_PC = (@r_PC + 1) & 65535;
            SET @r_R = (@r_R & 128) | ((@r_R + 1) & 127);
            SET @ts = @ts + 4;
        END;

        IF @ixy = 1
        BEGIN
            SET @t = @r_H; SET @r_H = @r_IXH; SET @r_IXH = @t;
            SET @t = @r_L; SET @r_L = @r_IXL; SET @r_IXL = @t;
        END
        ELSE IF @ixy = 2
        BEGIN
            SET @t = @r_H; SET @r_H = @r_IYH; SET @r_IYH = @t;
            SET @t = @r_L; SET @r_L = @r_IYL; SET @r_IYL = @t;
        END;
"""

FOOTER = r"""
        IF @ixy = 1
        BEGIN
            SET @t = @r_H; SET @r_H = @r_IXH; SET @r_IXH = @t;
            SET @t = @r_L; SET @r_L = @r_IXL; SET @r_IXL = @t;
        END
        ELSE IF @ixy = 2
        BEGIN
            SET @t = @r_H; SET @r_H = @r_IYH; SET @r_IYH = @t;
            SET @t = @r_L; SET @r_L = @r_IYL; SET @r_IYL = @t;
        END;

        /* EI defers interrupt acceptance by one instruction. That delay
           has to expire here, at the end of an instruction. Clearing it at
           the start of the next call -- after the interrupt gate has
           already been tested -- suppresses every interrupt that follows a
           frame ending in EI/HALT, which halves the machine's speed. */
        IF @ei_pending = 2
        BEGIN SET @ei_pending = 0; END
        ELSE IF @ei_pending = 1
        BEGIN SET @ei_pending = 2; END

        /* Breakpoint. Sampling PC between frames is useless -- a return
           address is current for exactly one instruction -- so check it
           here. @stop_pc NULL never compares equal, so this is free. */
        IF @r_PC = @stop_pc
        BEGIN
            SET @stopped = 1;
            BREAK;
        END;

        /* HALT: nothing can wake the CPU until the next interrupt, which
           this model raises at the frame boundary. Re-executing HALT to
           burn out the budget is faithful but pointless -- it was about
           80% of all instructions executed. Charge the remaining budget
           and stop. */
        IF @halt = 1
        BEGIN
            SET @ts = @budget;
            BREAK;
        END;
    END;

    UPDATE dbo.z80_state SET
        r_A = @r_A, r_F = @r_F, r_B = @r_B, r_C = @r_C,
        r_D = @r_D, r_E = @r_E, r_H = @r_H, r_L = @r_L,
        r_A2 = @r_A2, r_F2 = @r_F2, r_B2 = @r_B2, r_C2 = @r_C2,
        r_D2 = @r_D2, r_E2 = @r_E2, r_H2 = @r_H2, r_L2 = @r_L2,
        r_IXH = @r_IXH, r_IXL = @r_IXL, r_IYH = @r_IYH, r_IYL = @r_IYL,
        r_I = @r_I, r_R = @r_R, r_SP = @r_SP, r_PC = @r_PC,
        iff1 = @iff1, iff2 = @iff2, im = @im, halt = @halt,
        ei_pending = @ei_pending, border = @border,
        frames = frames + 1
    WHERE id = 1;

    SET @out_ts = @ts;
    SET @out_instr = @instr;
    SET @out_stopped = @stopped;
END;
GO
"""

def gen_cb_block():
    o = []
    def e(s): o.append('        ' + s)
    e('/* ---- CB prefix ---- */')
    e('IF @ixy <> 0')
    e('BEGIN')
    e('    SET @ea = @r_PC;')
    e('    SELECT @n8 = val FROM dbo.z80_mem WHERE addr = @ea;')
    e('    SET @r_PC = (@r_PC + 1) & 65535;')
    e('    SET @d = CASE WHEN @n8 > 127 THEN @n8 - 256 ELSE @n8 END;')
    e('    SET @ea2 = (@r_H*256 + @r_L + @d) & 65535;')
    e('    SET @ea = @r_PC;')
    e('    SELECT @op = val FROM dbo.z80_mem WHERE addr = @ea;')
    e('    SET @r_PC = (@r_PC + 1) & 65535;')
    e('    SET @memop = 1;')
    e('    SET @ts = @ts + 12;')
    e('END')
    e('ELSE')
    e('BEGIN')
    e('    SET @ea = @r_PC;')
    e('    SELECT @op = val FROM dbo.z80_mem WHERE addr = @ea;')
    e('    SET @r_PC = (@r_PC + 1) & 65535;')
    e('    SET @r_R = (@r_R & 128) | ((@r_R + 1) & 127);')
    e('    SET @ea2 = @r_H*256 + @r_L;')
    e('    SET @memop = CASE WHEN (@op & 7) = 6 THEN 1 ELSE 0 END;')
    e('    SET @ts = @ts + 4;')
    e('END;')
    e('SET @cbz = @op & 7;')
    e('SET @cby = (@op / 8) & 7;')
    e('SET @cbx = (@op / 64) & 3;')
    e('SET @bm = CASE @cby WHEN 0 THEN 1 WHEN 1 THEN 2 WHEN 2 THEN 4 WHEN 3 THEN 8'
      ' WHEN 4 THEN 16 WHEN 5 THEN 32 WHEN 6 THEN 64 ELSE 128 END;')
    # operand load
    e('IF @memop = 1')
    e('BEGIN')
    e('    SELECT @v = val FROM dbo.z80_mem WHERE addr = @ea2;')
    e('    SET @ts = @ts + 4;')
    e('END')
    for i in range(8):
        if i == 6:
            continue
        e(f'ELSE IF @cbz = {i}')
        e(f'BEGIN SET @v = {real_r(i)}; END')
    e('')
    e('IF @cbx = 0')
    e('BEGIN')
    o.append(gen_cb_ops())
    e('END')
    e('ELSE IF @cbx = 1')
    e('BEGIN')
    e('    -- BIT b,r')
    e('    SET @r_F = (@r_F & 1) | 16')
    e('             | (CASE WHEN (@v & @bm) = 0 THEN 68 ELSE 0 END)')
    e('             | (CASE WHEN @cby = 7 AND (@v & 128) <> 0 THEN 128 ELSE 0 END)')
    e('             | (@v & 40);')
    e('END')
    e('ELSE IF @cbx = 2')
    e('BEGIN SET @res = @v & (255 ^ @bm); END    -- RES b,r')
    e('ELSE')
    e('BEGIN SET @res = @v | @bm; END            -- SET b,r')
    e('')
    e('IF @cbx <> 1')
    e('BEGIN')
    e('    IF @memop = 1')
    e('    BEGIN')
    e('        IF @ea2 > 16383 UPDATE dbo.z80_mem SET val = @res & 255 WHERE addr = @ea2;')
    e('        SET @ts = @ts + 3;')
    e('    END;')
    e('    IF @cbz <> 6')
    e('    BEGIN')
    first = True
    for i in range(8):
        if i == 6:
            continue
        e(f'        {"IF" if first else "ELSE IF"} @cbz = {i}')
        first = False
        e('        BEGIN')
        L.clear()
        set_real_r(i, '@res')
        for ln in flush().splitlines():
            e('            ' + ln.strip())
        e('        END')
    e('    END;')
    e('END;')
    return '\n'.join(o)

def main():
    base = {}
    for op in range(256):
        base[op] = gen_base(op)
    ed = {}
    for op in range(256):
        ed[op] = gen_ed(op)

    out = [HEADER]
    out.append('        IF @op = 203')
    out.append('        BEGIN')
    out.append(gen_cb_block())
    out.append('        END')
    out.append('        ELSE IF @op = 237')
    out.append('        BEGIN')
    out.append('            SET @ea = @r_PC;')
    out.append('            SELECT @op = val FROM dbo.z80_mem WHERE addr = @ea;')
    out.append('            SET @r_PC = (@r_PC + 1) & 65535;')
    out.append('            SET @r_R = (@r_R & 128) | ((@r_R + 1) & 127);')
    out.append('            SET @ts = @ts + 4;')
    out.append('            SET @r_F_old = @r_F;')
    out.append(tree(ed, 0, 256, 3))
    out.append('        END')
    out.append('        ELSE')
    out.append('        BEGIN')
    out.append(tree(base, 0, 256, 3))
    out.append('        END;')
    out.append(FOOTER)

    sql = '\n'.join(out)

    # A build stamp, so "which copy is on the server?" is a query rather
    # than an argument. The hash covers the generated body only.
    import hashlib
    build = hashlib.sha256(sql.encode()).hexdigest()[:12]
    sql = sql.replace('DO NOT EDIT BY HAND.',
                      f'DO NOT EDIT BY HAND.\n   BUILD {build}', 1)
    sql += f'''
/* ---- build stamp -------------------------------------------------- */
IF OBJECT_ID('dbo.z80_build') IS NULL
    CREATE TABLE dbo.z80_build
    (
        id        tinyint      NOT NULL PRIMARY KEY,
        build     varchar(40)  NOT NULL,
        loaded_at datetime2(0) NOT NULL
    );
GO
DELETE FROM dbo.z80_build;
INSERT INTO dbo.z80_build (id, build, loaded_at) VALUES (1, '{build}', SYSDATETIME());
PRINT 'z80 core build {build} loaded';
GO
'''
    with open('sql/02_z80_core.sql', 'w') as fh:
        fh.write(sql)
    print(f'wrote sql/02_z80_core.sql  {len(sql):,} bytes  '
          f'{sql.count(chr(10)):,} lines  build {build}')

if __name__ == '__main__':
    main()
