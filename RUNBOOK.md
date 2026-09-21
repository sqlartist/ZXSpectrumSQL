# Runbook

From an empty database to a scored 100m. Roughly fifteen minutes, most of
it waiting on the ROM's memory test.

Prerequisites: SQL Server 2017 or later (for `STRING_AGG ... WITHIN GROUP`),
Python 3.8+, `pyodbc`, and optionally Pillow for PNG export.

---

## 1. Deploy

Run in this order. Each of the two big files prints a build stamp — if you
don't see it, the file didn't load and nothing downstream is worth reading.

```sql
:r sql/01_schema.sql     -- ~30s, creates 65,536 memory rows
:r sql/02_z80_core.sql   -- prints: z80 core build 0628c71dc61f loaded
:r sql/03_driver.sql     -- prints: driver build 00d664036530 loaded
:r sql/00_rom.sql        -- prints: ROM loaded and verified (byte sum 1926175)
```

Confirm what's actually deployed:

```sql
SELECT id, build, loaded_at FROM dbo.z80_build ORDER BY id;
-- 1 = core, 2 = driver
```

The core takes a while to compile. 8,200 lines, 14 levels of nesting. One-off
per deploy, not per call.

---

## 2. Test the CPU

```sql
:r sql/04_smoke.sql
```

Four lines, all `PASS`:

```
executed 48 instructions in 620 T-states  (48 = correct, stops at HALT)
A/DJNZ: PASS | DD+RLCA: PASS | LDIR: PASS
breakpoint: PASS (37 instructions, 506 T-states)
```

A hand-assembled program exercising `LDIR`, `DJNZ`, indexed addressing under
a `DD` prefix, a conditional relative jump and a rotate — plus a breakpoint
check that stops mid-program and asserts a state that exists for exactly one
instruction.

Don't go further until these pass.

---

## 3. Boot the ROM

```sql
EXEC dbo.z80_reset;
EXEC dbo.z80_run_frames @frames = 5;     -- throughput check
EXEC dbo.z80_boot @frames = 150;
```

Expect `ROM initialised` and **RAMTOP 65367** — the correct constant for a
48K with no Interface 1, and only reachable if the memory test completed
properly.

Be patient. The ROM's RAM check fills 49,152 bytes then walks back over every
one of them: about 640,000 instructions, 5.7M T-states, **82 frames** before
it even finishes.

See the copyright message without leaving SQL:

```sql
WITH bits AS (SELECT i FROM dbo.z80_nums WHERE i < 8)
SELECT s.y,
       STRING_AGG(CAST(CASE WHEN (m.val & CAST(POWER(2, b.i) AS int)) <> 0
                            THEN '#' ELSE '.' END AS varchar(max)), '')
           WITHIN GROUP (ORDER BY s.xbyte, b.i DESC) AS scanline
FROM dbo.z80_scr_addr s
JOIN dbo.z80_mem m ON m.addr = s.addr
CROSS JOIN bits b
WHERE s.y BETWEEN 176 AND 191
GROUP BY s.y ORDER BY s.y;
```

---

## 4. Read the cartridge

```powershell
python client/mdr_extract.py DaleyThompsonsDecathlon.mdr
```

Catalogue plus a detokenised listing of the BASIC loader. For the Ocean
cartridge you get a BASIC program per day and two CODE blocks each.

Generate the launch sequence:

```powershell
python client/mdr_extract.py DaleyThompsonsDecathlon.mdr --game "Day 1" --sql 05_game.sql
python tsql_lint.py 05_game.sql
```

It parses the `CLEAR` value and the `RANDOMIZE USR` chain straight out of the
tokenised BASIC, so it isn't hardcoded to one release.

Useful extras:

```powershell
python client/mdr_extract.py <mdr> --hex "Day 1"
python client/mdr_extract.py <mdr> --dis "Day 1" --at 23999 --count 60
python client/mdr_extract.py <mdr> --extract blocks/
```

---

## 5. Load the game

```sql
:r 05_game.sql
```

Boots, sets RAMTOP, loads the BASIC program and both CODE blocks, calls the
depacker, and enters at the launch routine. A few minutes.

Watch for `z80_call 32179` reporting **`returned`** in single-digit frames.
That's Ocean's loading-screen depacker: 88 bytes of code expanding 3,611
bytes into all 6,912 of the display file, attributes interleaved with bitmap,
one character cell at a time.

The script refuses to run against a stale deployment — it checks
`sys.parameters` for `@stop_pc` and `sys.sql_modules` for the driver, and
sets `NOEXEC ON` rather than spending three minutes producing a wrong answer.

---

## 6. Navigate to an event

Don't hardcode frame numbers. `z80_wait_static` runs until the display file
has been unchanged for N frames, which is what "waiting for input" looks
like from outside.

```sql
DELETE FROM dbo.z80_input_trace;

EXEC dbo.z80_wait_static;                       -- title
EXEC dbo.z80_key   @key = '2', @nframes = 5;    -- 2 = Kempston
EXEC dbo.z80_wait_static;                       -- name entry
EXEC dbo.z80_stick @bits = 16, @nframes = 4;    -- fire: first letter
EXEC dbo.z80_wait_static @stable = 10;
EXEC dbo.z80_stick @bits = 16, @nframes = 4;
EXEC dbo.z80_wait_static @stable = 10;
EXEC dbo.z80_stick @bits = 16, @nframes = 4;
EXEC dbo.z80_wait_static;                       -- DAY 1 card
EXEC dbo.z80_stick @bits = 16, @nframes = 4;
EXEC dbo.z80_wait_static;                       -- start line
```

Press-and-release matters. Several screens wait for a key to come *back up*
before they proceed, so hold for a handful of frames and no more.

Kempston bits: 1 right, 2 left, 4 down, 8 up, 16 fire. They combine.

---

## 7. Run the event

```sql
EXEC dbo.z80_snap @slot = 1;
EXEC dbo.z80_waggle @nframes = 700, @hz = 11.0;
EXEC dbo.z80_run_frames @frames = 700, @capture = 1, @quiet = 1;
EXEC dbo.z80_snap @slot = 2;
EXEC dbo.z80_diff @a = 1, @b = 2;
```

700 frames is 14 emulated seconds, past the 13.50 qualifying standard, and
about 30 seconds of wall clock.

`z80_waggle` produces strict exclusive alternation — one direction per frame,
never both, never neither. That matters: the game's interrupt handler
penalises any frame where both or neither are held.

---

## 8. See it

```powershell
python client/spectrum_client.py --dsn "$DSN" --export frames/   # PNGs
python client/spectrum_client.py --dsn "$DSN" --replay           # live, 50fps
python client/spectrum_client.py --dsn "$DSN" --ascii            # no GUI
```

`--export` is usually the one you want; static images beat a 50fps window
when you're working out what a screen is doing.

Or from SQL alone:

```sql
EXEC dbo.z80_frame_text @y0 = 176, @y1 = 191, @x0 = 96, @x1 = 255;
```

---

## Debugging

**Where is it?**

```sql
EXEC dbo.z80_regs;
EXEC dbo.z80_mem_hex @from = 43760, @len = 96;
```

Feed the hex to the disassembler:

```powershell
python client/z80dis.py block.bin 0xAAF0 0 40
```

**Is it stuck or waiting?** Take three register dumps a frame apart. Same PC
with `B` counting down is a `HALT`/`DJNZ` delay loop, not a hang.

**Is it reading input at all?**

```sql
EXEC dbo.z80_probe_in @port = 31;    -- Kempston
EXEC dbo.z80_probe_in @port = 254;   -- keyboard
```

Breakpoints every `IN` site in RAM and reports which ones execute. Costs a
couple of frames per candidate, so run it early in an event.

**What changed, and when?**

```sql
DELETE FROM dbo.z80_watch;
INSERT INTO dbo.z80_watch (addr, label) VALUES (43863, 'csec'), (33598, 'count');
EXEC dbo.z80_run_watch @frames = 60;
```

One row per frame. Slow — a separate call per frame — so use it for short
diagnostic windows, not for playing through an event.

---

## Addresses found so far

Day 1, from this cartridge. Others will differ.

| Address | What |
|---|---|
| `0xAB57` | Race clock, centiseconds, +2 per interrupt |
| `0xAAF7` | The in-race interrupt handler |
| `0xAB09`, `0xAB11`, `0xAB30`, `0xAB38` | Kempston reads in the waggle detector |
| `0x833E` | Frames since the last valid direction change, capped at 30 |
| `0x8340` | Which direction the game expects next |
| `0x6CA4`, `0x6CB7`, `0x6CCA` | Three athletes' screen positions, stride 19 |
| `0x5C78` | The ROM's `FRAMES` — only ticks while the game is in IM 1 |

Beware `0xFFE5`–`0xFFF5` in any diff: SP lives up there, so that's stack
churn, not variables.

---

## Gotchas

**Interrupt mode is a state signal.** The game switches to IM 2 during
events. If the ROM's `FRAMES` at `0x5C78` stops advancing, you're in game
code; if it moves, you're in a menu. Two diffs where one moved and the other
didn't were taken in different states and can't be compared.

**Absent trace rows mean keys up.** They latch otherwise, and a stuck key
hangs any wait-for-release loop.

**`EXEC` won't take an expression.** `@f + 20` needs a variable first.

**Frame numbers reset on boot**, and `z80_boot` truncates the capture and
trace tables so old frames can't shadow new ones.
