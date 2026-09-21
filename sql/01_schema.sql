/* =====================================================================
   01_schema.sql -- ZX Spectrum 48K in SQL Server: storage

   Run order: 01_schema -> 02_z80_core -> 03_driver -> 00_rom
   ===================================================================== */
SET NOCOUNT ON;
GO

DROP TABLE IF EXISTS dbo.z80_frame_capture;
DROP TABLE IF EXISTS dbo.z80_input_trace;
DROP TABLE IF EXISTS dbo.z80_scr_addr;
DROP TABLE IF EXISTS dbo.z80_kb;
DROP TABLE IF EXISTS dbo.z80_szp;
DROP TABLE IF EXISTS dbo.z80_state;
DROP TABLE IF EXISTS dbo.z80_mem;
DROP TABLE IF EXISTS dbo.z80_nums;
GO

/* ---- a tally table, 0..65535 -------------------------------------- */
CREATE TABLE dbo.z80_nums (i int NOT NULL PRIMARY KEY CLUSTERED);
GO
INSERT INTO dbo.z80_nums (i)
SELECT TOP (65536) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

/* ---- 64K address space --------------------------------------------
   0x0000-0x3FFF ROM (write-protected in the core)
   0x4000-0x5AFF display file + attributes
   0x5B00-0xFFFF RAM
   -------------------------------------------------------------------- */
CREATE TABLE dbo.z80_mem
(
    addr int      NOT NULL,
    val  tinyint  NOT NULL DEFAULT 0,
    CONSTRAINT PK_z80_mem PRIMARY KEY CLUSTERED (addr)
) WITH (DATA_COMPRESSION = NONE);
GO
INSERT INTO dbo.z80_mem (addr, val) SELECT i, 0 FROM dbo.z80_nums;
GO

/* ---- CPU state ------------------------------------------------------ */
CREATE TABLE dbo.z80_state
(
    id          tinyint NOT NULL PRIMARY KEY DEFAULT 1,
    r_A tinyint NOT NULL DEFAULT 0, r_F tinyint NOT NULL DEFAULT 0,
    r_B tinyint NOT NULL DEFAULT 0, r_C tinyint NOT NULL DEFAULT 0,
    r_D tinyint NOT NULL DEFAULT 0, r_E tinyint NOT NULL DEFAULT 0,
    r_H tinyint NOT NULL DEFAULT 0, r_L tinyint NOT NULL DEFAULT 0,
    r_A2 tinyint NOT NULL DEFAULT 0, r_F2 tinyint NOT NULL DEFAULT 0,
    r_B2 tinyint NOT NULL DEFAULT 0, r_C2 tinyint NOT NULL DEFAULT 0,
    r_D2 tinyint NOT NULL DEFAULT 0, r_E2 tinyint NOT NULL DEFAULT 0,
    r_H2 tinyint NOT NULL DEFAULT 0, r_L2 tinyint NOT NULL DEFAULT 0,
    r_IXH tinyint NOT NULL DEFAULT 0, r_IXL tinyint NOT NULL DEFAULT 0,
    r_IYH tinyint NOT NULL DEFAULT 92, r_IYL tinyint NOT NULL DEFAULT 58,
    r_I tinyint NOT NULL DEFAULT 63, r_R tinyint NOT NULL DEFAULT 0,
    r_SP int NOT NULL DEFAULT 65535,
    r_PC int NOT NULL DEFAULT 0,
    iff1 tinyint NOT NULL DEFAULT 0,
    iff2 tinyint NOT NULL DEFAULT 0,
    im   tinyint NOT NULL DEFAULT 1,
    halt tinyint NOT NULL DEFAULT 0,
    ei_pending tinyint NOT NULL DEFAULT 0,
    border   tinyint NOT NULL DEFAULT 7,
    kempston tinyint NOT NULL DEFAULT 0,
    frames   int NOT NULL DEFAULT 0
);
GO
INSERT INTO dbo.z80_state (id) VALUES (1);
GO

/* ---- S/Z/5/3/P flag lookup ------------------------------------------
   Parity is the only flag not cheaply derivable inline, so the whole
   S Z F5 F3 P/V group comes from here in one seek.
   -------------------------------------------------------------------- */
CREATE TABLE dbo.z80_szp
(
    v tinyint NOT NULL PRIMARY KEY CLUSTERED,
    f tinyint NOT NULL
);
GO
WITH bits AS (SELECT i FROM dbo.z80_nums WHERE i < 256)
INSERT INTO dbo.z80_szp (v, f)
SELECT i,
       CAST(
           (CASE WHEN i = 0   THEN 64 ELSE 0 END)      -- Z
         + (i & 128)                                   -- S
         + (i & 40)                                    -- F5, F3
         + (CASE WHEN (
               (i&1) + ((i/2)&1) + ((i/4)&1) + ((i/8)&1)
             + ((i/16)&1) + ((i/32)&1) + ((i/64)&1) + ((i/128)&1)
             ) % 2 = 0 THEN 4 ELSE 0 END)              -- P (even parity)
       AS tinyint)
FROM bits;
GO

/* ---- keyboard matrix: 8 half-rows, bits 0-4, 0 = pressed ------------ */
CREATE TABLE dbo.z80_kb
(
    row  tinyint NOT NULL PRIMARY KEY CLUSTERED,
    mask int     NOT NULL,   -- which address line selects this half-row
    val  tinyint NOT NULL DEFAULT 31,
    keys varchar(16) NOT NULL
);
GO
INSERT INTO dbo.z80_kb (row, mask, val, keys) VALUES
    (0, 1,   31, 'CS,Z,X,C,V'),
    (1, 2,   31, 'A,S,D,F,G'),
    (2, 4,   31, 'Q,W,E,R,T'),
    (3, 8,   31, '1,2,3,4,5'),
    (4, 16,  31, '0,9,8,7,6'),
    (5, 32,  31, 'P,O,I,U,Y'),
    (6, 64,  31, 'EN,L,K,J,H'),
    (7, 128, 31, 'SP,SS,M,N,B');
GO

/* ---- display file address decode ------------------------------------
   The Spectrum's third/character-row/pixel-row interleave, precomputed.
   Rendering a frame is then a join, not a loop.
   -------------------------------------------------------------------- */
CREATE TABLE dbo.z80_scr_addr
(
    addr      int NOT NULL PRIMARY KEY CLUSTERED,
    y         int NOT NULL,
    xbyte     int NOT NULL,
    attr_addr int NOT NULL,
    INDEX IX_z80_scr_addr_yx (y, xbyte)
);
GO
INSERT INTO dbo.z80_scr_addr (addr, y, xbyte, attr_addr)
SELECT 16384 + ((y.i & 192) * 32) + ((y.i & 7) * 256) + ((y.i & 56) * 4) + x.i,
       y.i, x.i,
       22528 + (y.i / 8) * 32 + x.i
FROM dbo.z80_nums y CROSS JOIN dbo.z80_nums x
WHERE y.i < 192 AND x.i < 32;
GO

/* ---- input trace ----------------------------------------------------
   This is the point of the whole design. Rather than sampling a live
   keyboard at whatever speed the emulator happens to run, we hand it a
   pre-recorded per-frame input stream and let it run as slowly as it
   likes. Waggle fidelity stops depending on emulator throughput.
   -------------------------------------------------------------------- */
CREATE TABLE dbo.z80_input_trace
(
    frame_no int     NOT NULL PRIMARY KEY CLUSTERED,
    kempston tinyint NOT NULL DEFAULT 0,   -- 000FUDLR, 1 = pressed
    kb0 tinyint NOT NULL DEFAULT 31, kb1 tinyint NOT NULL DEFAULT 31,
    kb2 tinyint NOT NULL DEFAULT 31, kb3 tinyint NOT NULL DEFAULT 31,
    kb4 tinyint NOT NULL DEFAULT 31, kb5 tinyint NOT NULL DEFAULT 31,
    kb6 tinyint NOT NULL DEFAULT 31, kb7 tinyint NOT NULL DEFAULT 31
);
GO

/* ---- captured frames ------------------------------------------------ */
CREATE TABLE dbo.z80_frame_capture
(
    frame_no int NOT NULL PRIMARY KEY CLUSTERED,
    border   tinyint NOT NULL,
    scr      varbinary(6912) NOT NULL
);
GO

PRINT 'schema ready';
GO
