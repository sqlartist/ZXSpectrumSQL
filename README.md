# Spectrum-in-SQL

> **Read the full story:** For the full breakdown of how this project came to be and the initial metrics, check out the article on [Medium](https://medium.com/@trevor.dwyer/zx-spectrum-48k-in-sql-server-running-daley-thompsons-decathlon-e5abfd1f3c3d). Follow me there for future updates! You can also read it free on [my blog](https://sqlartist.github.io/posts/zx-spectrum-48k-in-sql-server-running-daley-thompsons-decathlon/).


A ZX Spectrum 48K implemented inside SQL Server, running *Daley Thompson's
Decathlon* (Ocean, 1984) from its original Microdrive cartridge.

The database is the machine. Memory is a table. The CPU is a stored
procedure. The display file is decoded by a join. Input arrives as a
pre-recorded per-frame trace rather than a live poll.

It boots the real Sinclair ROM to the copyright message, runs Ocean's
loading-screen depacker, and plays the game at roughly half real time.

---

## Contents

| File | What it is |
|---|---|
| `sql/00_rom.sql` | The Sinclair 48K ROM, CRC32 `0xDDEE531F`, as a loadable blob |
| `sql/01_schema.sql` | 64K address space, CPU state, flag/keyboard/screen lookups |
| `sql/02_z80_core.sql` | **Generated.** The Z80 interpreter — one procedure, ~8,200 lines |
| `sql/03_driver.sql` | Machine control, I/O, display, input, debugging tools |
| `sql/04_smoke.sql` | Known-answer test for the core. Run this first |
| `sql/05_game.sql` | **Generated** from your cartridge by `mdr_extract.py --game` |
| `gen_z80.py` | Emits `02_z80_core.sql`. Edit this, never the SQL |
| `tsql_lint.py` | Catches the T-SQL faults that only surface at parse time |
| `client/mdr_extract.py` | Microdrive cartridge reader, BASIC detokeniser, disassembly |
| `client/mdr_game.py` | Turns a cartridge's BASIC loader into a SQL launch sequence |
| `client/z80dis.py` | Z80 disassembler, verified against the smoke-test program |
| `client/sqlblob.py` | Emits large binaries as valid chunked T-SQL |
| `client/spectrum_client.py` | Frame replay, PNG export, ASCII render, waggle recorder |

Current builds: core `0628c71dc61f`, driver `00d664036530`. Both stamp
`dbo.z80_build` on load, so `SELECT * FROM dbo.z80_build` tells you what is
actually deployed.

---

## What you need to supply

**The game.** Everything else is here. You want either a Microdrive `.mdr`
cartridge image or a 48K `.sna` snapshot, from your own copy.

The Microdrive route is better: the cartridge is unprotected, so the CODE
blocks can be lifted straight out and dropped into `dbo.z80_mem`. No tape,
no Speedlock, no Interface 1 emulation. See `RUNBOOK.md`.

The 48K ROM is already in `00_rom.sql` — Amstrad permit redistribution of
the Sinclair ROMs for emulation, so it can live in source control.

---

## How it works

**Memory** is `dbo.z80_mem (addr, val)`, 65,536 rows. Writes below 0x4000
are dropped; that is the ROM.

**The CPU** is `dbo.z80_run`, a generated procedure holding the register
file in scalar variables and dispatching on the opcode through a balanced
binary tree — eight comparisons per instruction rather than a 256-way `IF`.
State is flushed to `dbo.z80_state` at entry and exit only, so a call should
cover at least a whole frame to amortise it.

**The display** is just memory, so rendering is nearly free: a frame is one
`STRING_AGG` over 6,912 rows, or one join if you want it pure
(`dbo.z80_screen_pixels`).

**Input** is `dbo.z80_input_trace`, one row per frame. This is the part the
whole design bends around — see below.

---

## The input model

Decathlon is a reflex game. Every event is decided by waggle frequency
sampled at 50Hz. If the emulator runs at half speed, live play is
meaningless, because your wrist runs at wall-clock speed and the machine
does not.

So input is decoupled from execution. The trace holds one row per frame,
recorded at a true 50Hz or generated synthetically, and the emulator
consumes it at whatever rate it manages. Waggle fidelity stops depending on
throughput.

The interesting consequence: `dbo.z80_waggle` is parameterised by rate, so
an event's outcome becomes a function of `@hz` — something you can search
over from T-SQL.

---

## Performance

Roughly 50% of real time on a normal instance, having started at 9%.

| Change | Effect |
|---|---|
| Skip the frame's remainder on `HALT` | 9.7% → 48% |
| Fix the `EI` interrupt-delay flag | corrected a 2x speed error |

The `HALT` finding is the instructive one. The emulator was faithfully
re-executing `HALT` about thirteen thousand times a frame — perfectly
correct, and 80% of all work. One behavioural observation bought 5x, where
the memory-optimised table and native compilation everyone reaches for first
would have been days of work for maybe 10x.

If you do want more, in order of payoff: memory-optimised `z80_mem` with a
natively compiled `z80_run`; then batching operand fetches, since most
instructions are 1–3 bytes and each currently costs a separate seek.

---

## Known gaps

- **Frame-accurate, not cycle-accurate.** Interrupts fire at the frame
  boundary; T-state costs are approximated per memory access. Fine for this
  game, wrong for anything doing raster tricks.
- **No contended memory**, so the machine runs slightly fast in emulated
  terms.
- **No beeper.** Port 0xFE writes update the border only.
- **ED block I/O** (`INI`/`OUTI` and repeats) falls through to NOP.
- **`DD`/`FD`** work by swapping H/L with IXh/IXl around one instruction.
  `EX DE,HL` and the `LD r,(IX+d)` forms are special-cased; `DD` immediately
  followed by `ED` is not.
- **Tape and Speedlock** are not implemented, by design.

---

## Debugging tools

Built while chasing real bugs, and the reason the project got anywhere:

| Proc | What it does |
|---|---|
| `z80_regs` | Full register dump |
| `z80_mem_hex` | Live memory as paste-friendly hex, for `z80dis.py` |
| `z80_frame_text` | Any captured frame rendered as ASCII |
| `z80_snap` / `z80_diff` | Snapshot RAM and diff — finds variables without source |
| `z80_watch` / `z80_run_watch` | Log chosen addresses every frame |
| `z80_probe_in` | Breakpoint every `IN` site to find which one is live |
| `z80_wait_static` | Run until the screen stops changing |
| `z80_call` | Call an address and stop when it returns |

`z80_run` takes `@stop_pc` — a real breakpoint, tested after every
instruction. Comparing against `NULL` is never true, so it costs nothing
when unused.

---

## Building

```bash
python gen_z80.py && python tsql_lint.py sql/*.sql
```

The linter exits non-zero, so a bad generation cannot quietly ship. It knows
about orphaned `ELSE`, empty blocks, split binary literals, `RAISERROR`
arguments and block imbalance — every class of failure this project hit.
