#!/usr/bin/env python3
"""
mdr_extract.py -- read a ZX Interface 1 Microdrive cartridge image.

An .mdr is 254 fixed 543-byte records plus a trailing write-protect byte.
Each record is:

    offset  size  what
    0       1     HDFLAG   bit0 set = this is a header block
    1       1     HDNUMB   sector number, 1..254
    2       2     unused
    4       10    HDNAME   cartridge name
    14      1     HDCHK    sum of bytes 0..13, mod 255
    15      1     RECFLG   bit0 clear, bit1 = EOF, bit2 = PRINT file
    16      1     RECNUM   record number within the file, 0-based
    17      2     RECLEN   bytes of data in this record, 0..512
    19      10    RECNAM   file name
    29      1     DESCHK   sum of bytes 15..28, mod 255
    30      512   data
    542     1     DCHK     sum of the 512 data bytes, mod 255

Records are scattered across the cartridge in physical order, so files are
reassembled by (RECNAM, RECNUM). The first record of a file starts with a
9-byte header. NOTE this is NOT laid out like a tape header:

    0    type: 0 BASIC, 1 number array, 2 char array, 3 CODE
    1-2  HD_LEN   length of the data
    3-4  HD_ADD   start address (CODE), or PROG (BASIC)
    5-6  HD_VARS  offset to the variables area (BASIC)
    7-8  HD_LINE  autostart line, >= 32768 for none

Usage:
    python mdr_extract.py DaleyThompsonsDecathlon.mdr            # catalogue
    python mdr_extract.py Daley...mdr --extract out/             # dump files
    python mdr_extract.py Daley...mdr --sql sql/05_game.sql      # loader
"""

import argparse
import os
import sys

REC = 543
FILE_TYPE = {0: 'BASIC', 1: 'number array', 2: 'char array', 3: 'CODE'}

TOKENS = [
    'RND', 'INKEY$', 'PI', 'FN ', 'POINT ', 'SCREEN$ ', 'ATTR ', 'AT ', 'TAB ',
    'VAL$ ', 'CODE ', 'VAL ', 'LEN ', 'SIN ', 'COS ', 'TAN ', 'ASN ', 'ACS ',
    'ATN ', 'LN ', 'EXP ', 'INT ', 'SQR ', 'SGN ', 'ABS ', 'PEEK ', 'IN ',
    'USR ', 'STR$ ', 'CHR$ ', 'NOT ', 'BIN ', 'OR ', 'AND ', '<=', '>=', '<>',
    'LINE ', 'THEN ', 'TO ', 'STEP ', 'DEF FN ', 'CAT ', 'FORMAT ', 'MOVE ',
    'ERASE ', 'OPEN #', 'CLOSE #', 'MERGE ', 'VERIFY ', 'BEEP ', 'CIRCLE ',
    'INK ', 'PAPER ', 'FLASH ', 'BRIGHT ', 'INVERSE ', 'OVER ', 'OUT ',
    'LPRINT ', 'LLIST ', 'STOP ', 'READ ', 'DATA ', 'RESTORE ', 'NEW ',
    'BORDER ', 'CONTINUE ', 'DIM ', 'REM ', 'FOR ', 'GO TO ', 'GO SUB ',
    'INPUT ', 'LOAD ', 'LIST ', 'LET ', 'PAUSE ', 'NEXT ', 'POKE ', 'PRINT ',
    'PLOT ', 'RUN ', 'SAVE ', 'RANDOMIZE ', 'IF ', 'CLS ', 'DRAW ', 'CLEAR ',
    'RETURN ', 'COPY ',
]
assert len(TOKENS) == 256 - 165, len(TOKENS)


def csum(b):
    return sum(b) % 255


def parse(path):
    raw = open(path, 'rb').read()
    n = len(raw) // REC
    print(f'{os.path.basename(path)}: {len(raw):,} bytes, {n} records'
          f'{"" if len(raw) % REC in (0, 1) else " (odd trailing bytes)"}')

    cart, recs, bad = set(), [], 0
    for i in range(n):
        r = raw[i * REC:(i + 1) * REC]
        if not r[0] & 1:
            continue                       # not a valid header block
        if csum(r[0:14]) != r[14]:
            bad += 1
            continue
        cart.add(r[4:14].decode('latin-1').rstrip())
        if r[15] & 1:
            continue                       # header block only, no data record
        if csum(r[15:29]) != r[29]:
            bad += 1
            continue
        reclen = r[17] | (r[18] << 8)
        recs.append({
            'sector': r[1],
            'eof': bool(r[15] & 2),
            'print': bool(r[15] & 4),
            'num': r[16],
            'len': reclen,
            'name': r[19:29].decode('latin-1').rstrip(),
            'data': r[30:30 + reclen],
            'dchk_ok': csum(r[30:542]) == r[542],
        })

    if cart:
        print(f'cartridge name: {" / ".join(sorted(cart))}')
    if bad:
        print(f'{bad} records failed their header checksum (skipped)')
    return recs


def assemble(recs):
    files = {}
    for r in recs:
        files.setdefault(r['name'], []).append(r)

    out = []
    for name, parts in sorted(files.items()):
        parts.sort(key=lambda p: p['num'])
        seen, blob, gaps = set(), bytearray(), []
        for p in parts:
            if p['num'] in seen:
                continue
            seen.add(p['num'])
            blob += p['data']
        expect = max(seen) + 1 if seen else 0
        gaps = sorted(set(range(expect)) - seen)

        f = {'name': name, 'records': len(seen), 'gaps': gaps,
             'raw': bytes(blob), 'bad_data': [p['num'] for p in parts
                                              if not p['dchk_ok']]}
        if len(blob) >= 9:
            h = blob[:9]
            f['type'] = h[0]
            f['length'] = h[1] | (h[2] << 8)
            f['addr'] = h[3] | (h[4] << 8)      # HD_ADD
            f['vars'] = h[5] | (h[6] << 8)      # HD_VARS
            f['line'] = h[7] | (h[8] << 8)      # HD_LINE
            f['body'] = bytes(blob[9:9 + f['length']])
        else:
            f['type'] = None
            f['body'] = bytes(blob)
        out.append(f)
    return out


def detokenise(prog):
    """Spectrum BASIC program bytes -> listing text."""
    lines, i = [], 0
    while i + 4 <= len(prog):
        num = (prog[i] << 8) | prog[i + 1]          # line number is big-endian
        ln = prog[i + 2] | (prog[i + 3] << 8)       # length is little-endian
        if num > 9999 or ln == 0 or i + 4 + ln > len(prog):
            break
        body, j, text = prog[i + 4:i + 4 + ln], 0, []
        while j < len(body):
            c = body[j]
            if c == 0x0E:            # embedded 5-byte binary number, skip
                j += 6
                continue
            if c == 0x0D:
                j += 1
                continue
            if c >= 165:
                text.append(TOKENS[c - 165])
            elif 16 <= c <= 21:      # INK/PAPER/FLASH/BRIGHT/INVERSE/OVER
                text.append(f'{{{["INK","PAPER","FLASH","BRIGHT","INVERSE","OVER"][c-16]} {body[j+1] if j+1 < len(body) else "?"}}}')
                j += 1
            elif c in (22, 23):      # AT / TAB, two parameters
                text.append('{AT}' if c == 22 else '{TAB}')
                j += 2
            elif 32 <= c < 127:
                text.append(chr(c))
            else:
                text.append(f'{{{c}}}')
            j += 1
        lines.append(f'{num:5d} ' + ''.join(text))
        i += 4 + ln
    return lines


def report(files):
    print()
    print(f'{"file":<12} {"type":<13} {"len":>7} {"loads at":>9} {"autostart":>10} {"recs":>5}')
    print('-' * 72)
    for f in files:
        t = FILE_TYPE.get(f['type'], f'? ({f["type"]})')
        a = f.get('addr', 0)
        n = f.get('length', len(f['body']))
        auto = f.get('line', 0)
        extra = f'   {a:5d}..{a + n - 1:5d}  0x{a:04X}-0x{a + n - 1:04X}' if n else ''
        print(f'{f["name"]:<12} {t:<13} {n:>7} {a:>9} '
              f'{("none" if auto >= 32768 else auto):>10} {f["records"]:>5}{extra}')
        if f['gaps']:
            print(f'    !! missing records: {f["gaps"]}')
        if f['bad_data']:
            print(f'    !! data checksum failed on records: {f["bad_data"]}')

    for f in files:
        if f['type'] == 0:
            a = f.get('addr', 0)
            print(f'\n--- BASIC listing: {f["name"]} '
                  f'(PROG at {a}, variables at {a + f.get("vars", 0)}) ---')
            for line in detokenise(f['body']):
                print(line)


def emit_sql(files, path, entry=None):
    parts = ["""/* =====================================================================
   05_game.sql -- GENERATED by mdr_extract.py from a Microdrive cartridge.

   Loads the CODE blocks straight into dbo.z80_mem. No tape, no Speedlock,
   no Interface 1: the BASIC loader's job was only ever to place these
   blocks and jump, so we do that directly.

   Run order:
       EXEC dbo.z80_boot;      -- reset + ~150 frames of ROM init
       :r sql/05_game.sql      -- this file
   ===================================================================== */
SET NOCOUNT ON;
GO
"""]
    for f in files:
        if f['type'] != 3:
            continue
        from sqlblob import emit_blob
        v = _ident(f['name'])
        parts.append(
            f"\n/* {f['name']}: {len(f['body'])} bytes at 0x{f['addr']:04X} */\n"
            + emit_blob(f'@{v}', f['body'])
            + f"EXEC dbo.z80_load_bin @data = @{v}, @addr = {f['addr']};\nGO\n")
    if entry:
        parts.append(f"""
/* jump to the entry point the BASIC loader would have used */
EXEC dbo.z80_start_code @entry = {entry};
GO
""")
    else:
        parts.append("""
/* No entry point supplied. Find the RANDOMIZE USR nnnnn in the BASIC
   listing above and run:   EXEC dbo.z80_start_code @entry = nnnnn;      */
""")
    open(path, 'w').write(''.join(parts))
    print(f'\nwrote {path}')


def _ident(name):
    return ''.join(c if c.isalnum() else '_' for c in name) or 'blk'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mdr')
    ap.add_argument('--extract', metavar='DIR', help='dump each file to DIR')
    ap.add_argument('--sql', metavar='FILE', help='emit a SQL loader')
    ap.add_argument('--entry', type=int, help='entry address for the SQL loader')
    ap.add_argument('--dis', metavar='NAME', help='disassemble a file')
    ap.add_argument('--at', type=int, help='absolute address to disassemble from')
    ap.add_argument('--count', type=int, default=48, help='instructions to show')
    ap.add_argument('--hex', metavar='NAME', help='hex dump a file')
    ap.add_argument('--game', metavar='DAY',
                    help='emit a full launch sequence, e.g. --game "Day 1"')
    a = ap.parse_args()

    files = assemble(parse(a.mdr))
    if not files:
        sys.exit('no readable files -- is this a 48K Microdrive image?')
    report(files)

    if a.extract:
        os.makedirs(a.extract, exist_ok=True)
        for f in files:
            p = os.path.join(a.extract, _ident(f['name']) +
                             ('.bin' if f['type'] == 3 else '.dat'))
            open(p, 'wb').write(f['body'])
            print(f'wrote {p} ({len(f["body"])} bytes)')

    byname = {f['name']: f for f in files}

    if a.hex:
        f = byname.get(a.hex) or sys.exit(f'no file named {a.hex!r}')
        base = f.get('addr', 0)
        b = f['body']
        for o in range(0, len(b), 16):
            chunk = b[o:o + 16]
            txt = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
            print(f'{base + o:04X}  {chunk.hex(" ").upper():<47}  {txt}')

    if a.dis:
        from z80dis import disassemble
        f = byname.get(a.dis) or sys.exit(f'no file named {a.dis!r}')
        base = f.get('addr', 0)
        start = (a.at - base) if a.at is not None else 0
        if not 0 <= start < len(f['body']):
            sys.exit(f'{a.at} is outside {a.dis!r} '
                     f'({base}..{base + len(f["body"]) - 1})')
        print(f'\n--- {a.dis} disassembled from {base + start} '
              f'(0x{base + start:04X}) ---')
        print('\n'.join(disassemble(f['body'], org=base, start=start,
                                    count=a.count)))

    if a.game:
        from mdr_game import emit_game
        emit_game(files, a.sql or 'sql/05_game.sql', a.game, detokenise)
    elif a.sql:
        emit_sql(files, a.sql, a.entry)


if __name__ == '__main__':
    main()
