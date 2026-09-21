#!/usr/bin/env python3
"""
tsql_lint.py -- catch the structural mistakes that only surface when
SQL Server parses 7,500 generated lines.

Checks, in order of how much time they cost me:

  1. ORPHANED ELSE   -- a ';' between a statement and its ELSE terminates
                        the IF, so the ELSE has nothing to attach to.
                        'IF x SET @a=1; ELSE SET @a=2;' is a syntax error;
                        'IF x SET @a=1 ELSE SET @a=2;' is fine, and
                        'IF x BEGIN SET @a=1; END ELSE ...' is better.
  2. END; ELSE       -- the same thing with a block body.
  3. EMPTY BLOCK     -- BEGIN followed only by comments then END. T-SQL
                        requires at least one statement.
  4. SPLIT LITERAL  -- a 0x binary literal broken across lines. Literals
                       cannot contain whitespace, so wrapping 32k hex
                       digits for readability makes the script invalid.
  5. RAISERROR ARG  -- substitution arguments must be constants or
                       variables. RAISERROR('%d', 16, 1, DATALENGTH(@x))
                       is a syntax error.
  6. UNBALANCED      -- BEGIN/CASE vs END across the file.

Usage:  python tsql_lint.py sql/*.sql
"""

import re
import sys


def strip_comments(text):
    """Blank out -- comments and /* */ blocks, preserving line structure."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith('--', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i))
            i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(''.join(c if c == '\n' else ' ' for c in text[i:j]))
            i = j
        elif text[i] == "'":
            j = i + 1
            while j < n and text[j] != "'":
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def _raiserror_args(src):
    """Yield (position, [top-level args]) for every RAISERROR( ... ) call."""
    for m in re.finditer(r'\bRAISERROR\s*\(', src, re.I):
        i, depth, start = m.end(), 1, m.end()
        while i < len(src) and depth:
            if src[i] == '(':
                depth += 1
            elif src[i] == ')':
                depth -= 1
            i += 1
        inner = src[start:i - 1]
        args, depth, cur = [], 0, ''
        for ch in inner:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if ch == ',' and depth == 0:
                args.append(cur)
                cur = ''
            else:
                cur += ch
        args.append(cur)
        yield m.start(), args


def lineno(text, pos):
    return text.count('\n', 0, pos) + 1


def check(path):
    raw = open(path).read()
    src = strip_comments(raw)
    problems = []

    for m in re.finditer(r';\s*\bELSE\b', src, re.I):
        problems.append((lineno(src, m.start()), 'ORPHANED ELSE',
                         "';' before ELSE terminates the IF"))

    for pos, args in _raiserror_args(src):
        for a in args[3:]:
            if re.search(r'\w\s*\(', a):
                problems.append((lineno(src, pos), 'RAISERROR ARG',
                                 'substitution args must be constants or '
                                 f'variables, not {a.strip()[:40]}'))

    for m in re.finditer(r'0[xX][0-9A-Fa-f]*\s*\n\s*[0-9A-Fa-f]', src):
        problems.append((lineno(src, m.start()), 'SPLIT LITERAL',
                         'binary literal wrapped across lines'))

    for m in re.finditer(r'\bBEGIN\b\s*\bEND\b', src, re.I):
        problems.append((lineno(src, m.start()), 'EMPTY BLOCK',
                         'BEGIN...END with no statement'))

    depth = 0
    for m in re.finditer(r'\b(BEGIN|CASE|END)\b', src, re.I):
        depth += 1 if m.group(1).upper() in ('BEGIN', 'CASE') else -1
        if depth < 0:
            problems.append((lineno(src, m.start()), 'UNBALANCED',
                             'END with no matching BEGIN/CASE'))
            depth = 0
    if depth:
        problems.append((0, 'UNBALANCED', f'{depth} unclosed BEGIN/CASE'))

    problems.sort()
    if problems:
        print(f'{path}: {len(problems)} problem(s)')
        for ln, kind, msg in problems[:20]:
            print(f'  line {ln:>6}  {kind:<14} {msg}')
        if len(problems) > 20:
            print(f'  ... and {len(problems) - 20} more')
    else:
        print(f'{path}: clean')
    return len(problems)


if __name__ == '__main__':
    total = sum(check(p) for p in sys.argv[1:])
    print(f'\n{total} problem(s) total')
    sys.exit(1 if total else 0)
