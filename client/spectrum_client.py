#!/usr/bin/env python3
"""
spectrum_client.py -- a thin front end for the SQL Server Spectrum.

The database is the machine. This does three things and nothing else:
  * shovels a .sna into dbo.z80_mem / dbo.z80_state
  * records a waggle trace at wall-clock 50Hz into dbo.z80_input_trace
  * pulls captured frames back out and draws them

Because input is a recorded trace rather than a live poll, the emulator's
throughput is decoupled from the game's timing. Record at 50Hz, execute at
whatever the box manages, play back at 50Hz.

    pip install pyodbc

    python spectrum_client.py --dsn "..." --sna daley.sna --load
    python spectrum_client.py --dsn "..." --bench 50
    python spectrum_client.py --dsn "..." --waggle 11 --frames 600 --run
    python spectrum_client.py --dsn "..." --record 600
    python spectrum_client.py --dsn "..." --replay
"""

import argparse
import os
import sys
import time

try:
    import pyodbc
except ImportError:
    sys.exit('pyodbc required:  pip install pyodbc')

PALETTE = [
    (0, 0, 0), (0, 0, 215), (215, 0, 0), (215, 0, 215),
    (0, 215, 0), (0, 215, 215), (215, 215, 0), (215, 215, 215),
    (0, 0, 0), (0, 0, 255), (255, 0, 0), (255, 0, 255),
    (0, 255, 0), (0, 255, 255), (255, 255, 0), (255, 255, 255),
]

# Kempston bits: 0 right, 1 left, 2 down, 3 up, 4 fire
KEMPSTON = {'Right': 1, 'Left': 2, 'Down': 4, 'Up': 8, 'space': 16}


def scr_offset(y, xbyte):
    return ((y & 0xC0) << 5) | ((y & 0x07) << 8) | ((y & 0x38) << 2) | xbyte


OFFSETS = [[scr_offset(y, xb) for xb in range(32)] for y in range(192)]


def decode_rgb(scr, flash_phase=False):
    """6912 display-file bytes -> 256x192 raw RGB."""
    out = bytearray()
    for y in range(192):
        arow = 6144 + (y >> 3) * 32
        for xb in range(32):
            bits = scr[OFFSETS[y][xb]]
            attr = scr[arow + xb]
            ink = (attr & 7) | ((attr >> 3) & 8)
            paper = ((attr >> 3) & 7) | ((attr >> 3) & 8)
            if attr & 0x80 and flash_phase:
                ink, paper = paper, ink
            ci, cp = PALETTE[ink], PALETTE[paper]
            for b in range(7, -1, -1):
                out += bytes(ci if bits & (1 << b) else cp)
    return bytes(out)


def decode(scr, flash_phase=False):
    """6912 display-file bytes -> a binary P6 PPM."""
    return b'P6\n256 192\n255\n' + decode_rgb(scr, flash_phase)


def decode_ascii(scr, step=4):
    """Every `step`th scanline as text. No GUI, no dependencies."""
    lines = []
    for y in range(0, 192, step):
        row = []
        for xb in range(32):
            bits = scr[OFFSETS[y][xb]]
            for b in range(7, -1, -1):
                row.append('#' if bits & (1 << b) else '.')
        lines.append(''.join(row))
    return lines


# ------------------------------------------------------------------ db

def connect(dsn):
    cn = pyodbc.connect(dsn, autocommit=True)
    cn.timeout = 0
    return cn


def load_sna(cn, path):
    data = open(path, 'rb').read()
    if len(data) != 49179:
        sys.exit(f'{path}: expected a 49179-byte 48K .sna, got {len(data)}')
    cur = cn.cursor()
    cur.execute('EXEC dbo.z80_load_sna ?', pyodbc.Binary(data))
    row = cur.fetchone()
    print(f'loaded: PC={row.pc:04X} SP={row.sp:04X} IM{row.im} border={row.border}')


def bench(cn, frames):
    cur = cn.cursor()
    t0 = time.time()
    cur.execute('EXEC dbo.z80_run_frames ?, 0, 0', frames)
    row = cur.fetchone()
    wall = time.time() - t0
    print(f'{row.frames_run} frames  {row.instructions:,} instructions')
    print(f'server {row.elapsed_ms} ms  wall {wall*1000:.0f} ms')
    print(f'{row.instr_per_sec:,.0f} instr/sec   '
          f'{row.effective_fps} fps   {row.pct_realtime}% of real time')


def push_trace(cn, trace, start):
    """trace: list of kempston bytes, one per frame."""
    cur = cn.cursor()
    cur.execute('DELETE FROM dbo.z80_input_trace WHERE frame_no >= ?', start)
    cur.fast_executemany = True
    cur.executemany(
        'INSERT INTO dbo.z80_input_trace (frame_no, kempston) VALUES (?, ?)',
        [(start + i, v) for i, v in enumerate(trace)])
    print(f'{len(trace)} frames of input written from frame {start}')


def current_frame(cn):
    return cn.cursor().execute(
        'SELECT frames FROM dbo.z80_state WHERE id = 1').fetchone()[0]


def fetch_captured(cn):
    cur = cn.cursor().execute(
        'SELECT frame_no, border, scr FROM dbo.z80_frame_capture ORDER BY frame_no')
    return [(r.frame_no, r.border, bytes(r.scr)) for r in cur]


# --------------------------------------------------------------- record

def record(cn, nframes):
    """
    Capture keystate at wall-clock 50Hz. Your wrist runs in real time even
    though the machine underneath it will not.
    """
    import tkinter as tk

    held, trace = set(), []
    root = tk.Tk()
    root.title('waggle recorder -- arrow keys / space, Esc to stop')
    lbl = tk.Label(root, font=('TkFixedFont', 18), width=34, height=6)
    lbl.pack()

    def down(e):
        if e.keysym == 'Escape':
            root.quit()
        held.add(e.keysym)

    def up(e):
        held.discard(e.keysym)

    root.bind('<KeyPress>', down)
    root.bind('<KeyRelease>', up)

    t0 = time.perf_counter()

    def tick():
        n = len(trace)
        target = t0 + (n + 1) / 50.0
        v = 0
        for k in held:
            v |= KEMPSTON.get(k, 0)
        trace.append(v)
        if n % 10 == 0:
            hz = 0.0
            if n > 50:
                w = trace[-50:]
                hz = sum(1 for i in range(1, 50) if w[i] != w[i - 1])
            lbl.config(text=f'frame {n}/{nframes}\nkempston {v:05b}\n~{hz} changes/sec')
        if n + 1 >= nframes:
            root.quit()
            return
        delay = max(1, int((target - time.perf_counter()) * 1000))
        root.after(delay, tick)

    root.after(0, tick)
    root.mainloop()
    root.destroy()
    return trace


# --------------------------------------------------------------- replay

def replay(cn, fps=50, scale=2):
    """
    Play captured frames back at 50fps.

    Tk's PhotoImage only reliably accepts base64 GIF via data=, not PPM, so
    use Pillow when it is there and fall back to writing a PPM to a temp
    file, which Tk reads happily.
    """
    import tkinter as tk

    frames = fetch_captured(cn)
    if not frames:
        sys.exit('nothing in dbo.z80_frame_capture -- run with --capture first')
    print(f'{len(frames)} captured frames')

    try:
        from PIL import Image, ImageTk
        have_pil = True
    except ImportError:
        have_pil = False
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), 'zxframe.ppm')
        print('Pillow not found -- using the slower temp-file path')

    root = tk.Tk()
    root.title('Spectrum (SQL Server)')
    canvas = tk.Label(root, bd=0)
    canvas.pack()
    state = {'i': 0}

    def tick():
        i = state['i'] % len(frames)
        fno, border, scr = frames[i]
        flash = (fno // 16) % 2 == 1
        if have_pil:
            img = Image.frombytes('RGB', (256, 192), decode_rgb(scr, flash))
            img = img.resize((256 * scale, 192 * scale), Image.NEAREST)
            photo = ImageTk.PhotoImage(img)
        else:
            with open(tmp, 'wb') as fh:
                fh.write(decode(scr, flash))
            photo = tk.PhotoImage(file=tmp).zoom(scale)
        canvas.configure(image=photo)
        canvas.image = photo               # keep a reference or Tk frees it
        root.title(f'Spectrum (SQL Server) -- frame {fno}')
        state['i'] = i + 1
        root.after(int(1000 / fps), tick)

    root.after(0, tick)
    root.mainloop()


def export(cn, outdir, fmt='png'):
    """Write captured frames to disk. PNG via Pillow, PPM without it."""
    frames = fetch_captured(cn)
    if not frames:
        sys.exit('nothing in dbo.z80_frame_capture -- run with --capture first')
    os.makedirs(outdir, exist_ok=True)
    try:
        from PIL import Image
    except ImportError:
        Image = None
        if fmt == 'png':
            print('Pillow not found -- writing PPM instead')
            fmt = 'ppm'
    for fno, border, scr in frames:
        rgb = decode_rgb(scr, (fno // 16) % 2 == 1)
        path = os.path.join(outdir, f'frame_{fno:06d}.{fmt}')
        if fmt == 'png':
            Image.frombytes('RGB', (256, 192), rgb).save(path)
        else:
            with open(path, 'wb') as fh:
                fh.write(b'P6\n256 192\n255\n' + rgb)
    print(f'wrote {len(frames)} {fmt.upper()} frames to {outdir}')


def show_ascii(cn, step=4):
    """Print the most recent captured frame as text."""
    frames = fetch_captured(cn)
    if not frames:
        sys.exit('nothing in dbo.z80_frame_capture -- run with --capture first')
    fno, border, scr = frames[-1]
    print(f'frame {fno}, border {border}')
    for line in decode_ascii(scr, step):
        print(line)


# ------------------------------------------------------------------ cli

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dsn', required=True,
                    help='pyodbc connection string')
    ap.add_argument('--sna', help='48K .sna snapshot to load')
    ap.add_argument('--load', action='store_true', help='load the snapshot')
    ap.add_argument('--reset', action='store_true', help='reset the machine')
    ap.add_argument('--bench', type=int, metavar='N', help='run N frames, report throughput')
    ap.add_argument('--waggle', type=float, metavar='HZ', help='synthetic waggle at HZ')
    ap.add_argument('--frames', type=int, default=500, help='trace / run length')
    ap.add_argument('--record', type=int, metavar='N', help='record N frames of live input')
    ap.add_argument('--run', action='store_true', help='execute the trace, capturing frames')
    ap.add_argument('--replay', action='store_true', help='play back captured frames')
    ap.add_argument('--scale', type=int, default=2, help='replay zoom factor')
    ap.add_argument('--export', metavar='DIR', help='write captured frames as images')
    ap.add_argument('--ascii', action='store_true',
                    help='print the last captured frame as text (no GUI needed)')
    a = ap.parse_args()

    cn = connect(a.dsn)

    if a.reset:
        cn.cursor().execute('EXEC dbo.z80_reset')
        print('reset')
    if a.load:
        if not a.sna:
            sys.exit('--load needs --sna')
        load_sna(cn, a.sna)
    if a.bench:
        bench(cn, a.bench)
    if a.waggle:
        start = current_frame(cn)
        cn.cursor().execute('EXEC dbo.z80_waggle ?, ?, ?', start, a.frames, a.waggle)
        print(f'synthetic {a.waggle}Hz waggle, {a.frames} frames from {start}')
    if a.record:
        start = current_frame(cn)
        push_trace(cn, record(cn, a.record), start)
    if a.run:
        t0 = time.time()
        cur = cn.cursor()
        cur.execute('EXEC dbo.z80_run_frames ?, 1, 0', a.frames)
        row = cur.fetchone()
        print(f'{row.frames_run} frames in {time.time()-t0:.1f}s wall  '
              f'({row.pct_realtime}% of real time)')
    if a.export:
        export(cn, a.export)
    if a.ascii:
        show_ascii(cn)
    if a.replay:
        replay(cn, scale=a.scale)


if __name__ == '__main__':
    main()
