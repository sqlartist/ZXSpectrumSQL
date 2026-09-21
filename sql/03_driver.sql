/* =====================================================================
   03_driver.sql -- machine control, I/O plumbing and the display
   ===================================================================== */
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

/* ---------------------------------------------------------------- reset */
CREATE OR ALTER PROCEDURE dbo.z80_reset
    @wipe_ram bit = 1
AS
BEGIN
    SET NOCOUNT ON;
    IF @wipe_ram = 1
        UPDATE dbo.z80_mem SET val = 0 WHERE addr > 16383;

    UPDATE dbo.z80_state SET
        r_A = 255, r_F = 255, r_B = 0, r_C = 0, r_D = 0, r_E = 0,
        r_H = 0, r_L = 0,
        r_A2 = 0, r_F2 = 0, r_B2 = 0, r_C2 = 0, r_D2 = 0, r_E2 = 0,
        r_H2 = 0, r_L2 = 0,
        r_IXH = 0, r_IXL = 0, r_IYH = 92, r_IYL = 58,
        r_I = 63, r_R = 0,
        r_SP = 65535, r_PC = 0,
        iff1 = 0, iff2 = 0, im = 1, halt = 0, ei_pending = 0,
        border = 7, kempston = 0, frames = 0
    WHERE id = 1;

    UPDATE dbo.z80_kb SET val = 31;
    PRINT 'machine reset, PC = 0000';
END;
GO

/* ------------------------------------------------- load a raw block */
CREATE OR ALTER PROCEDURE dbo.z80_load_bin
    @data      varbinary(max),
    @addr      int,
    @allow_rom bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @len int = DATALENGTH(@data);

    UPDATE m
       SET m.val = CONVERT(tinyint, SUBSTRING(@data, n.i + 1, 1))
      FROM dbo.z80_mem m
      JOIN dbo.z80_nums n ON m.addr = @addr + n.i
     WHERE n.i < @len
       AND (@allow_rom = 1 OR m.addr > 16383);

    PRINT CONCAT('loaded ', @len, ' bytes at ', @addr);
END;
GO

/* -------------------------------------------------- load a 48K .sna
   27-byte header then 49152 bytes of RAM. PC is on the stack, as if the
   snapshot were taken inside an interrupt, so we pop it.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_load_sna
    @sna varbinary(max)
AS
BEGIN
    SET NOCOUNT ON;
    IF DATALENGTH(@sna) < 49179
    BEGIN
        RAISERROR('not a 48K .sna (expected 49179 bytes)', 16, 1);
        RETURN;
    END;

    DECLARE @b tinyint;
    DECLARE @byte TABLE (o int PRIMARY KEY, v tinyint);
    INSERT INTO @byte (o, v)
    SELECT n.i, CONVERT(tinyint, SUBSTRING(@sna, n.i + 1, 1))
    FROM dbo.z80_nums n WHERE n.i < 27;

    UPDATE s SET
        r_I   = (SELECT v FROM @byte WHERE o = 0),
        r_L2  = (SELECT v FROM @byte WHERE o = 1),
        r_H2  = (SELECT v FROM @byte WHERE o = 2),
        r_E2  = (SELECT v FROM @byte WHERE o = 3),
        r_D2  = (SELECT v FROM @byte WHERE o = 4),
        r_C2  = (SELECT v FROM @byte WHERE o = 5),
        r_B2  = (SELECT v FROM @byte WHERE o = 6),
        r_F2  = (SELECT v FROM @byte WHERE o = 7),
        r_A2  = (SELECT v FROM @byte WHERE o = 8),
        r_L   = (SELECT v FROM @byte WHERE o = 9),
        r_H   = (SELECT v FROM @byte WHERE o = 10),
        r_E   = (SELECT v FROM @byte WHERE o = 11),
        r_D   = (SELECT v FROM @byte WHERE o = 12),
        r_C   = (SELECT v FROM @byte WHERE o = 13),
        r_B   = (SELECT v FROM @byte WHERE o = 14),
        r_IYL = (SELECT v FROM @byte WHERE o = 15),
        r_IYH = (SELECT v FROM @byte WHERE o = 16),
        r_IXL = (SELECT v FROM @byte WHERE o = 17),
        r_IXH = (SELECT v FROM @byte WHERE o = 18),
        iff2  = CASE WHEN ((SELECT v FROM @byte WHERE o = 19) & 4) <> 0 THEN 1 ELSE 0 END,
        iff1  = CASE WHEN ((SELECT v FROM @byte WHERE o = 19) & 4) <> 0 THEN 1 ELSE 0 END,
        r_R   = (SELECT v FROM @byte WHERE o = 20),
        r_F   = (SELECT v FROM @byte WHERE o = 21),
        r_A   = (SELECT v FROM @byte WHERE o = 22),
        r_SP  = (SELECT v FROM @byte WHERE o = 24) * 256
              + (SELECT v FROM @byte WHERE o = 23),
        im     = (SELECT v FROM @byte WHERE o = 25) & 3,
        border = (SELECT v FROM @byte WHERE o = 26) & 7,
        halt = 0, ei_pending = 0, kempston = 0, frames = 0
      FROM dbo.z80_state s WHERE s.id = 1;

    UPDATE m
       SET m.val = CONVERT(tinyint, SUBSTRING(@sna, 28 + n.i, 1))
      FROM dbo.z80_mem m
      JOIN dbo.z80_nums n ON m.addr = 16384 + n.i
     WHERE n.i < 49152;

    /* PC = pop() */
    DECLARE @sp int, @lo tinyint, @hi tinyint;
    SELECT @sp = r_SP FROM dbo.z80_state WHERE id = 1;
    SELECT @lo = val FROM dbo.z80_mem WHERE addr = @sp;
    SELECT @hi = val FROM dbo.z80_mem WHERE addr = (@sp + 1) & 65535;
    UPDATE dbo.z80_state
       SET r_PC = @hi * 256 + @lo, r_SP = (@sp + 2) & 65535
     WHERE id = 1;

    UPDATE dbo.z80_kb SET val = 31;

    SELECT r_PC AS pc, r_SP AS sp, im, border FROM dbo.z80_state WHERE id = 1;
END;
GO

/* ------------------------------------------------------ screen readout
   6912 bytes of display file as one varbinary. This is the cheap path
   for a client; dbo.z80_screen_pixels below is the pure-SQL version.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_screen
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @hex varchar(max);
    SELECT @hex = STRING_AGG(CAST(CONVERT(char(2), CONVERT(binary(1), val), 2) AS varchar(max)), '')
                  WITHIN GROUP (ORDER BY addr)
    FROM dbo.z80_mem
    WHERE addr BETWEEN 16384 AND 23295;

    SELECT CONVERT(varbinary(max), @hex, 2) AS scr,
           (SELECT border FROM dbo.z80_state WHERE id = 1) AS border,
           (SELECT frames FROM dbo.z80_state WHERE id = 1) AS frames;
END;
GO

/* -------------------------------------- the renderer, as a single join */
CREATE OR ALTER VIEW dbo.z80_screen_pixels
AS
SELECT  s.y,
        s.xbyte * 8 + (7 - b.i)                       AS x,
        CASE WHEN (px.val & CAST(POWER(2, b.i) AS int)) <> 0
             THEN (at.val & 7)                        -- ink
             ELSE ((at.val / 8) & 7)                  -- paper
        END + ((at.val / 64) & 1) * 8                 AS colour,
        (at.val / 128) & 1                            AS flash
FROM        dbo.z80_scr_addr s
CROSS JOIN  (SELECT i FROM dbo.z80_nums WHERE i < 8) b
JOIN        dbo.z80_mem px ON px.addr = s.addr
JOIN        dbo.z80_mem at ON at.addr = s.attr_addr;
GO

/* --------------------------------------------------- run N frames */
CREATE OR ALTER PROCEDURE dbo.z80_run_frames
    @frames  int = 1,
    @capture bit = 0,
    @quiet   bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @i int = 0, @f int, @ts int, @instr int,
            @sum_ts bigint = 0, @sum_instr bigint = 0,
            @hex varchar(max),
            @t0 datetime2(3) = SYSDATETIME();

    WHILE @i < @frames
    BEGIN
        SELECT @f = frames FROM dbo.z80_state WHERE id = 1;

        /* apply this frame's slice of the input trace */
        UPDATE s SET s.kempston = ISNULL(t.kempston, 0)
          FROM dbo.z80_state s
          LEFT JOIN dbo.z80_input_trace t ON t.frame_no = @f
         WHERE s.id = 1;

        /* LEFT JOIN, not JOIN. With an inner join a frame that has no
           trace row leaves z80_kb holding the previous frame's value, so
           a keypress latches on for ever and any wait-for-release loop in
           the game hangs. No trace row means no keys down. */
        UPDATE k SET k.val = CASE k.row
                                WHEN 0 THEN ISNULL(t.kb0, 31) WHEN 1 THEN ISNULL(t.kb1, 31)
                                WHEN 2 THEN ISNULL(t.kb2, 31) WHEN 3 THEN ISNULL(t.kb3, 31)
                                WHEN 4 THEN ISNULL(t.kb4, 31) WHEN 5 THEN ISNULL(t.kb5, 31)
                                WHEN 6 THEN ISNULL(t.kb6, 31) ELSE ISNULL(t.kb7, 31) END
          FROM dbo.z80_kb k
          LEFT JOIN dbo.z80_input_trace t ON t.frame_no = @f;

        EXEC dbo.z80_run @budget = 69888, @assert_int = 1,
                         @out_ts = @ts OUTPUT, @out_instr = @instr OUTPUT;

        SET @sum_ts = @sum_ts + @ts;
        SET @sum_instr = @sum_instr + @instr;

        IF @capture = 1
        BEGIN
            SELECT @hex = STRING_AGG(CAST(CONVERT(char(2), CONVERT(binary(1), val), 2) AS varchar(max)), '')
                          WITHIN GROUP (ORDER BY addr)
            FROM dbo.z80_mem WHERE addr BETWEEN 16384 AND 23295;

            DELETE FROM dbo.z80_frame_capture WHERE frame_no = @f;
            INSERT INTO dbo.z80_frame_capture (frame_no, border, scr)
            SELECT @f, border, CONVERT(varbinary(6912), @hex, 2)
            FROM dbo.z80_state WHERE id = 1;
        END;

        SET @i = @i + 1;
    END;

    IF @quiet = 0
    BEGIN
        DECLARE @ms int = DATEDIFF(millisecond, @t0, SYSDATETIME());
        SELECT @frames                                        AS frames_run,
               @ms                                            AS elapsed_ms,
               @sum_instr                                     AS instructions,
               CAST(@sum_instr * 1000.0 / NULLIF(@ms, 0) AS decimal(12,1)) AS instr_per_sec,
               CAST(@frames * 1000.0 / NULLIF(@ms, 0) AS decimal(8,2))     AS effective_fps,
               CAST(@frames * 1000.0 / NULLIF(@ms, 0) / 50.0 * 100 AS decimal(6,2)) AS pct_realtime;
    END;
END;
GO

/* ------------------------------------------- build a waggle trace
   Alternate two Kempston directions at @hz, from @from_frame for
   @nframes. This is what replaces a human wrist.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_waggle
    @from_frame int = NULL,      -- NULL = start at the next frame to run
    @nframes    int = 600,
    @hz         decimal(6,2) = 10.0,
    @bit_a      tinyint = 1,     -- right
    @bit_b      tinyint = 2,     -- left
    @extra      tinyint = 0      -- OR'd in every frame (e.g. fire = 16)
AS
BEGIN
    SET NOCOUNT ON;

    IF @from_frame IS NULL
        SELECT @from_frame = frames FROM dbo.z80_state WHERE id = 1;

    DELETE FROM dbo.z80_input_trace
     WHERE frame_no BETWEEN @from_frame AND @from_frame + @nframes - 1;

    /* 50 frames per second, so a full A/B cycle is 50/@hz frames */
    INSERT INTO dbo.z80_input_trace (frame_no, kempston)
    SELECT @from_frame + n.i,
           CASE WHEN (CAST(n.i AS decimal(12,4)) * @hz * 2 / 50.0) % 2 < 1
                THEN @bit_a ELSE @bit_b END | @extra
    FROM dbo.z80_nums n
    WHERE n.i < @nframes;

    SELECT @from_frame AS from_frame, @nframes AS trace_frames, @hz AS hz,
           CAST(50.0 / NULLIF(@hz, 0) AS decimal(6,2)) AS frames_per_cycle;
END;
GO

/* ---------------------------------------------------- boot to BASIC
   Reset and run the ROM long enough to initialise the system variables,
   set up the interrupt vector and print the copyright message. Machine
   code loaded from a Microdrive was always entered from a live BASIC
   environment, so give it one.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_boot
    @frames int = 150
AS
BEGIN
    SET NOCOUNT ON;
    /* frame numbers restart from zero, so old captures would interleave
       with new ones and z80_frame_text would pick a stale frame. */
    TRUNCATE TABLE dbo.z80_frame_capture;
    DELETE FROM dbo.z80_input_trace;
    EXEC dbo.z80_reset @wipe_ram = 1;
    EXEC dbo.z80_run_frames @frames = @frames, @capture = 0, @quiet = 1;

    DECLARE @ramtop int, @pc int, @hl int, @glyphs int;
    SELECT @ramtop = (SELECT val FROM dbo.z80_mem WHERE addr = 23731) * 256
                   + (SELECT val FROM dbo.z80_mem WHERE addr = 23730);
    SELECT @pc = r_PC, @hl = r_H * 256 + r_L FROM dbo.z80_state WHERE id = 1;
    SELECT @glyphs = COUNT(*) FROM dbo.z80_mem
     WHERE addr BETWEEN 16384 AND 22527 AND val <> 0;

    /* The 48K ROM's RAM check is ~5.7M T-states, about 82 frames: it fills
       0x4000-0xFFFF then walks back over every byte. Landing inside
       0x11DA-0x11EE means it is still working, not broken -- HL is how far
       up memory the test has got. RAMTOP is not written until 0x11F1. */
    SELECT @frames AS booted_frames,
           @pc AS pc, @hl AS hl, @ramtop AS ramtop, @glyphs AS display_bytes_set,
           CASE
             WHEN @ramtop BETWEEN 20000 AND 65535 THEN 'ROM initialised'
             WHEN @pc BETWEEN 4570 AND 4590 THEN
                  CONCAT('still in the ROM RAM test (tested to 0x',
                         CONVERT(char(4), CONVERT(binary(2), @hl), 2),
                         ') - run more frames')
             WHEN @pc BETWEEN 4560 AND 4570 THEN
                  'still filling RAM - run more frames'
             ELSE CONCAT('unexpected PC 0x',
                         CONVERT(char(4), CONVERT(binary(2), @pc), 2),
                         ' - check the core')
           END AS status;
END;
GO

/* --------------------------------------------- enter machine code
   The BASIC equivalent of RANDOMIZE USR nnnnn: interrupts enabled, IM 1,
   a stack below the loaded block, PC at the entry point.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_start_code
    @entry int,
    @sp    int = NULL,
    @ei    bit = 1
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE dbo.z80_state
       SET r_PC = @entry & 65535,
           r_SP = ISNULL(@sp, r_SP),
           iff1 = CASE WHEN @ei = 1 THEN 1 ELSE 0 END,
           iff2 = CASE WHEN @ei = 1 THEN 1 ELSE 0 END,
           im   = 1,
           halt = 0,
           ei_pending = 0
     WHERE id = 1;

    SELECT r_PC AS pc, r_SP AS sp, iff1, im FROM dbo.z80_state WHERE id = 1;
END;
GO

/* -------------------------------------------- call and wait for RET
   The Microdrive loader is a sequence of RANDOMIZE USR calls with BASIC
   in between. With no Interface 1 to service the LOADs we drive that
   sequence ourselves: push a sentinel return address, run frames until
   PC lands on it, then move to the next stage.

   0x0000 is the ROM reset vector and nothing legitimate returns there, so
   it makes a safe sentinel -- but only with a real breakpoint. PC holds a
   return address for exactly one instruction, so checking it between
   frames misses it and the machine reboots through 0x0000 instead. Hence
   @stop_pc on z80_run, which tests PC after every instruction.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_call
    @addr       int,
    @max_frames int = 500,
    @sentinel   int = 0
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sp int, @pc int, @i int = 0, @ts int, @instr int;

    SELECT @sp = r_SP FROM dbo.z80_state WHERE id = 1;

    SET @sp = (@sp - 1) & 65535;
    IF @sp > 16383 UPDATE dbo.z80_mem SET val = (@sentinel / 256) & 255 WHERE addr = @sp;
    SET @sp = (@sp - 1) & 65535;
    IF @sp > 16383 UPDATE dbo.z80_mem SET val = @sentinel & 255 WHERE addr = @sp;

    UPDATE dbo.z80_state
       SET r_PC = @addr & 65535, r_SP = @sp, halt = 0
     WHERE id = 1;

    DECLARE @stopped bit = 0;
    WHILE @i < @max_frames
    BEGIN
        EXEC dbo.z80_run @budget = 69888, @assert_int = 1,
                         @stop_pc = @sentinel,
                         @out_ts = @ts OUTPUT, @out_instr = @instr OUTPUT,
                         @out_stopped = @stopped OUTPUT;
        SET @i = @i + 1;
        IF @stopped = 1 BREAK;
    END;

    SELECT @pc = r_PC FROM dbo.z80_state WHERE id = 1;

    SELECT @addr AS called, @i AS frames_taken, @pc AS pc,
           CASE WHEN @stopped = 1 THEN 'returned'
                ELSE 'did not return - still running or hung' END AS status;
END;
GO

PRINT 'driver ready';
GO

/* ---- the keyboard matrix, by name ---------------------------------- */
IF OBJECT_ID('dbo.z80_keymap') IS NULL
BEGIN
    CREATE TABLE dbo.z80_keymap
    (
        keyname varchar(6) NOT NULL PRIMARY KEY,
        kb_row  tinyint    NOT NULL,
        kb_bit  tinyint    NOT NULL
    );
    INSERT INTO dbo.z80_keymap (keyname, kb_row, kb_bit) VALUES
        ('CS',0,0),('Z',0,1),('X',0,2),('C',0,3),('V',0,4),
        ('A',1,0),('S',1,1),('D',1,2),('F',1,3),('G',1,4),
        ('Q',2,0),('W',2,1),('E',2,2),('R',2,3),('T',2,4),
        ('1',3,0),('2',3,1),('3',3,2),('4',3,3),('5',3,4),
        ('0',4,0),('9',4,1),('8',4,2),('7',4,3),('6',4,4),
        ('P',5,0),('O',5,1),('I',5,2),('U',5,3),('Y',5,4),
        ('ENTER',6,0),('L',6,1),('K',6,2),('J',6,3),('H',6,4),
        ('SPACE',7,0),('SS',7,1),('M',7,2),('N',7,3),('B',7,4);
END;
GO

/* -------------------------------------------- press a key for a while
   Writes a keypress into the input trace. Keys are active-low, so this
   clears the relevant bit and leaves any other key in that frame alone.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_key
    @key         varchar(6),
    @from_frame  int = NULL,      -- default: the next frame to run
    @nframes     int = 5
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @row tinyint, @bit tinyint, @mask int;

    SELECT @row = kb_row, @bit = kb_bit
      FROM dbo.z80_keymap WHERE keyname = UPPER(@key);

    IF @row IS NULL
    BEGIN
        RAISERROR('unknown key %s -- see dbo.z80_keymap', 16, 1, @key);
        RETURN;
    END;

    IF @from_frame IS NULL
        SELECT @from_frame = frames FROM dbo.z80_state WHERE id = 1;

    SET @mask = 31 ^ (CASE @bit WHEN 0 THEN 1 WHEN 1 THEN 2 WHEN 2 THEN 4
                                WHEN 3 THEN 8 ELSE 16 END);

    INSERT INTO dbo.z80_input_trace (frame_no)
    SELECT @from_frame + n.i
      FROM dbo.z80_nums n
     WHERE n.i < @nframes
       AND NOT EXISTS (SELECT 1 FROM dbo.z80_input_trace t
                        WHERE t.frame_no = @from_frame + n.i);

    UPDATE dbo.z80_input_trace
       SET kb0 = CASE WHEN @row = 0 THEN kb0 & @mask ELSE kb0 END,
           kb1 = CASE WHEN @row = 1 THEN kb1 & @mask ELSE kb1 END,
           kb2 = CASE WHEN @row = 2 THEN kb2 & @mask ELSE kb2 END,
           kb3 = CASE WHEN @row = 3 THEN kb3 & @mask ELSE kb3 END,
           kb4 = CASE WHEN @row = 4 THEN kb4 & @mask ELSE kb4 END,
           kb5 = CASE WHEN @row = 5 THEN kb5 & @mask ELSE kb5 END,
           kb6 = CASE WHEN @row = 6 THEN kb6 & @mask ELSE kb6 END,
           kb7 = CASE WHEN @row = 7 THEN kb7 & @mask ELSE kb7 END
     WHERE frame_no BETWEEN @from_frame AND @from_frame + @nframes - 1;

    SELECT UPPER(@key) AS pressed, @row AS kb_row, @bit AS kb_bit,
           @from_frame AS from_frame, @nframes AS held_frames;
END;
GO

/* ------------------------------------------ render a captured frame
   ASCII art from dbo.z80_frame_capture, so a counter can be read exactly
   rather than squinted at in a 256x192 PNG. Defaults to the whole screen;
   narrow it with the y/x window.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_frame_text
    @frame_no int = NULL,          -- NULL = the most recent capture
    @y0 int = 0,   @y1 int = 191,
    @x0 int = 0,   @x1 int = 255,
    @every int = 1                 -- take every Nth pixel column
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @scr varbinary(6912);

    IF @frame_no IS NULL
        SELECT TOP (1) @frame_no = frame_no
          FROM dbo.z80_frame_capture ORDER BY frame_no DESC;

    SELECT @scr = scr FROM dbo.z80_frame_capture WHERE frame_no = @frame_no;

    IF @scr IS NULL
    BEGIN
        RAISERROR('no captured frame %d', 16, 1, @frame_no);
        RETURN;
    END;

    ;WITH bits AS (SELECT i FROM dbo.z80_nums WHERE i < 8),
         px AS (
        SELECT s.y, s.xbyte * 8 + (7 - b.i) AS x,
               CASE WHEN (CONVERT(int, SUBSTRING(@scr, s.addr - 16383, 1))
                          & CAST(POWER(2, b.i) AS int)) <> 0
                    THEN '#' ELSE '.' END AS dot
          FROM dbo.z80_scr_addr s
         CROSS JOIN bits b
         WHERE s.y BETWEEN @y0 AND @y1
           AND s.xbyte * 8 + (7 - b.i) BETWEEN @x0 AND @x1
    )
    SELECT @frame_no AS frame_no, y,
           CAST(STRING_AGG(CAST(dot AS varchar(max)), '')
                WITHIN GROUP (ORDER BY x) AS varchar(256)) AS px
      FROM px
     WHERE (x - @x0) % @every = 0
     GROUP BY y
     ORDER BY y;
END;
GO

/* ------------------------------------------------ dump live memory
   16 bytes per row, paste-friendly, so a region of the running machine
   can be disassembled with client/z80dis.py.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_mem_hex
    @from int,
    @len  int = 128
AS
BEGIN
    SET NOCOUNT ON;
    SELECT CONVERT(char(4), CONVERT(binary(2), (@from + n.i * 16)), 2) AS addr,
           (SELECT STRING_AGG(CAST(CONVERT(char(2), CONVERT(binary(1), m.val), 2)
                              AS varchar(max)), ' ')
                   WITHIN GROUP (ORDER BY m.addr)
              FROM dbo.z80_mem m
             WHERE m.addr BETWEEN @from + n.i * 16 AND @from + n.i * 16 + 15) AS bytes
      FROM dbo.z80_nums n
     WHERE n.i < (@len + 15) / 16
     ORDER BY n.i;
END;
GO

/* ----------------------------------------------- full register dump */
CREATE OR ALTER PROCEDURE dbo.z80_regs
AS
BEGIN
    SET NOCOUNT ON;
    SELECT CONVERT(char(4), CONVERT(binary(2), r_PC), 2) AS pc,
           CONVERT(char(4), CONVERT(binary(2), r_SP), 2) AS sp,
           CONVERT(char(2), CONVERT(binary(1), r_A), 2)  AS a,
           CONVERT(char(2), CONVERT(binary(1), r_F), 2)  AS f,
           CONVERT(char(4), CONVERT(binary(2), r_B * 256 + r_C), 2) AS bc,
           CONVERT(char(4), CONVERT(binary(2), r_D * 256 + r_E), 2) AS de,
           CONVERT(char(4), CONVERT(binary(2), r_H * 256 + r_L), 2) AS hl,
           CONVERT(char(4), CONVERT(binary(2), r_IXH * 256 + r_IXL), 2) AS ix,
           CONVERT(char(4), CONVERT(binary(2), r_IYH * 256 + r_IYL), 2) AS iy,
           iff1, im, halt, border, kempston, frames
      FROM dbo.z80_state WHERE id = 1;
END;
GO

/* ------------------------------------------------- memory snapshots
   Take a copy of RAM at two moments and diff them. This is how you find
   a game's variables without its source: run a known interval, then look
   for the byte that moved by the amount you expected.
   -------------------------------------------------------------------- */
IF OBJECT_ID('dbo.z80_snapshot') IS NULL
    CREATE TABLE dbo.z80_snapshot
    (
        slot tinyint NOT NULL,
        addr int     NOT NULL,
        val  tinyint NOT NULL,
        CONSTRAINT PK_z80_snapshot PRIMARY KEY CLUSTERED (slot, addr)
    );
GO

CREATE OR ALTER PROCEDURE dbo.z80_snap
    @slot tinyint
AS
BEGIN
    SET NOCOUNT ON;
    DELETE FROM dbo.z80_snapshot WHERE slot = @slot;
    INSERT INTO dbo.z80_snapshot (slot, addr, val)
    SELECT @slot, addr, val FROM dbo.z80_mem WHERE addr > 16383;
    SELECT @slot AS slot, COUNT(*) AS bytes FROM dbo.z80_snapshot WHERE slot = @slot;
END;
GO

CREATE OR ALTER PROCEDURE dbo.z80_diff
    @a      tinyint,
    @b      tinyint,
    @from   int = 23296,      -- skip the display file by default
    @to     int = 65535,
    @change int = NULL        -- only bytes that moved by exactly this
AS
BEGIN
    SET NOCOUNT ON;
    SELECT CONVERT(char(4), CONVERT(binary(2), a.addr), 2) AS addr_hex,
           a.addr, a.val AS was, b.val AS now,
           CAST(b.val AS int) - CAST(a.val AS int) AS delta
      FROM dbo.z80_snapshot a
      JOIN dbo.z80_snapshot b ON b.slot = @b AND b.addr = a.addr
     WHERE a.slot = @a
       AND a.addr BETWEEN @from AND @to
       AND a.val <> b.val
       AND (@change IS NULL OR CAST(b.val AS int) - CAST(a.val AS int) = @change)
     ORDER BY a.addr;
END;
GO

/* ------------------------------------------------- push the joystick
   Kempston bits: 1 right, 2 left, 4 down, 8 up, 16 fire. Combine them
   freely (18 = left+fire). Writes into the input trace like z80_key, so
   absent frames still mean "released".
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_stick
    @bits       tinyint,
    @from_frame int = NULL,      -- NULL = the next frame to run
    @nframes    int = 5
AS
BEGIN
    SET NOCOUNT ON;

    IF @from_frame IS NULL
        SELECT @from_frame = frames FROM dbo.z80_state WHERE id = 1;

    INSERT INTO dbo.z80_input_trace (frame_no)
    SELECT @from_frame + n.i
      FROM dbo.z80_nums n
     WHERE n.i < @nframes
       AND NOT EXISTS (SELECT 1 FROM dbo.z80_input_trace t
                        WHERE t.frame_no = @from_frame + n.i);

    UPDATE dbo.z80_input_trace
       SET kempston = kempston | @bits
     WHERE frame_no BETWEEN @from_frame AND @from_frame + @nframes - 1;

    SELECT @bits AS bits,
           CONCAT(CASE WHEN @bits &  1 <> 0 THEN 'RIGHT ' ELSE '' END,
                  CASE WHEN @bits &  2 <> 0 THEN 'LEFT '  ELSE '' END,
                  CASE WHEN @bits &  4 <> 0 THEN 'DOWN '  ELSE '' END,
                  CASE WHEN @bits &  8 <> 0 THEN 'UP '    ELSE '' END,
                  CASE WHEN @bits & 16 <> 0 THEN 'FIRE'   ELSE '' END) AS pressed,
           @from_frame AS from_frame, @nframes AS held_frames;
END;
GO

/* ----------------------------------------------------- watch memory
   Run frames one at a time and log chosen addresses after each one. This
   is how you find out *when* something happened rather than guessing from
   the state at the end of a 700-frame run.
   -------------------------------------------------------------------- */
IF OBJECT_ID('dbo.z80_watch') IS NULL
    CREATE TABLE dbo.z80_watch
    (
        addr  int NOT NULL PRIMARY KEY,
        label varchar(30) NULL
    );
GO
IF OBJECT_ID('dbo.z80_watch_log') IS NULL
    CREATE TABLE dbo.z80_watch_log
    (
        frame_no int NOT NULL,
        addr     int NOT NULL,
        val      tinyint NOT NULL,
        CONSTRAINT PK_z80_watch_log PRIMARY KEY CLUSTERED (frame_no, addr)
    );
GO

CREATE OR ALTER PROCEDURE dbo.z80_run_watch
    @frames  int = 200,
    @capture bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @i int = 0, @f int, @ts int, @instr int;

    IF NOT EXISTS (SELECT 1 FROM dbo.z80_watch)
    BEGIN
        RAISERROR('dbo.z80_watch is empty -- add addresses first', 16, 1);
        RETURN;
    END;

    DELETE FROM dbo.z80_watch_log;

    WHILE @i < @frames
    BEGIN
        EXEC dbo.z80_run_frames @frames = 1, @capture = @capture, @quiet = 1;
        SELECT @f = frames FROM dbo.z80_state WHERE id = 1;

        INSERT INTO dbo.z80_watch_log (frame_no, addr, val)
        SELECT @f, w.addr, m.val
          FROM dbo.z80_watch w
          JOIN dbo.z80_mem m ON m.addr = w.addr;

        SET @i = @i + 1;
    END;

    /* one row per frame where anything moved */
    SELECT l.frame_no,
           MAX(CASE WHEN w.label IS NULL THEN NULL ELSE w.label END) AS x,
           STRING_AGG(CAST(CONCAT(ISNULL(w.label, CONVERT(varchar(8), l.addr)),
                                  '=', l.val) AS varchar(max)), '  ')
               WITHIN GROUP (ORDER BY l.addr) AS watch
      FROM dbo.z80_watch_log l
      JOIN dbo.z80_watch w ON w.addr = l.addr
     GROUP BY l.frame_no
     ORDER BY l.frame_no;
END;
GO

/* -------------------------------------------- which input site is live
   Finds every IN A,(n) for a given port in RAM and breakpoints each in
   turn, so you can see which one the game is actually executing in its
   current state. Costs @frames_each frames per candidate, so the machine
   does advance while you probe.

   @port 31 = Kempston, 254 = the keyboard/ULA.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_probe_in
    @port        tinyint = 31,
    @frames_each int = 2
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @cand TABLE (addr int PRIMARY KEY, hit bit NOT NULL DEFAULT 0);

    INSERT INTO @cand (addr)
    SELECT a.addr
      FROM dbo.z80_mem a
      JOIN dbo.z80_mem b ON b.addr = a.addr + 1
     WHERE a.val = 219 AND b.val = @port AND a.addr > 23295;

    DECLARE @a int, @ts int, @instr int, @stopped bit, @i int;
    DECLARE probe CURSOR LOCAL FAST_FORWARD FOR SELECT addr FROM @cand;
    OPEN probe;
    FETCH NEXT FROM probe INTO @a;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @stopped = 0;
        SET @i = 0;
        WHILE @i < @frames_each AND @stopped = 0
        BEGIN
            EXEC dbo.z80_run @budget = 69888, @assert_int = 1, @stop_pc = @a,
                             @out_ts = @ts OUTPUT, @out_instr = @instr OUTPUT,
                             @out_stopped = @stopped OUTPUT;
            SET @i = @i + 1;
        END;
        IF @stopped = 1 UPDATE @cand SET hit = 1 WHERE addr = @a;
        FETCH NEXT FROM probe INTO @a;
    END;
    CLOSE probe;
    DEALLOCATE probe;

    SELECT CONVERT(char(4), CONVERT(binary(2), addr), 2) AS at_hex,
           addr, hit AS executed
      FROM @cand
     ORDER BY hit DESC, addr;
END;
GO

/* ------------------------------------------- run until the screen stops
   Navigating by hardcoded frame numbers is fragile. Instead run frames
   until the display file has been unchanged for @stable frames, which is
   what "the game is waiting for input" looks like from outside. Makes a
   scripted run repeatable without knowing the timings in advance.
   -------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.z80_wait_static
    @max_frames int = 600,
    @stable     int = 25,
    @capture    bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @i int = 0, @same int = 0, @h bigint, @prev bigint = -1;

    WHILE @i < @max_frames AND @same < @stable
    BEGIN
        EXEC dbo.z80_run_frames @frames = 1, @capture = @capture, @quiet = 1;

        SELECT @h = SUM(CAST(val AS bigint) * (addr - 16383))
          FROM dbo.z80_mem WHERE addr BETWEEN 16384 AND 23295;

        IF @h = @prev
        BEGIN SET @same = @same + 1; END
        ELSE
        BEGIN SET @same = 0; END
        SET @prev = @h;
        SET @i = @i + 1;
    END;

    SELECT @i AS frames_run,
           (SELECT frames FROM dbo.z80_state WHERE id = 1) AS now_at_frame,
           @same AS stable_for,
           CASE WHEN @same >= @stable THEN 'settled -- waiting for input'
                ELSE 'still animating' END AS status;
END;
GO

/* ---- build stamp -------------------------------------------------- */
IF OBJECT_ID('dbo.z80_build') IS NULL
    CREATE TABLE dbo.z80_build
    (
        id        tinyint      NOT NULL PRIMARY KEY,
        build     varchar(40)  NOT NULL,
        loaded_at datetime2(0) NOT NULL
    );
GO
DELETE FROM dbo.z80_build WHERE id = 2;
INSERT INTO dbo.z80_build (id, build, loaded_at) VALUES (2, '00d664036530', SYSDATETIME());
PRINT 'driver build 00d664036530 loaded';
GO
