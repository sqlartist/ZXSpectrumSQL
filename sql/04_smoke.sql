/* =====================================================================
   04_smoke.sql -- known-answer test for the Z80 core.

   Run this before pointing the core at the ROM. 7,500 lines of generated
   T-SQL is not something you want to debug against a machine that fails
   silently, so exercise the awkward bits first: LDIR, DJNZ, indexed
   addressing under a DD prefix, a conditional relative jump and a rotate.

   Expected result: three PASS rows.
   ===================================================================== */
SET NOCOUNT ON;
GO

EXEC dbo.z80_reset @wipe_ram = 1;
GO

/*  8000  LD HL,9000        source
    8003  LD DE,9100        destination
    8006  LD BC,0010
    8009  LDIR              block copy 16 bytes
    800B  LD A,0F
    800D  LD B,08
    800F  ADD A,B           A = 0F + (8+7+..+1) = 33h
    8010  DJNZ 800F
    8012  LD (9200),A
    8015  LD IX,9300
    8019  LD (IX+5),A       DD-prefixed indexed write
    801C  LD A,00
    801E  LD A,(IX+5)       ...and read back
    8021  CP 33
    8023  JR NZ,802C
    8025  LD A,AA
    8027  RLCA              AAh -> 55h, carry set
    8028  LD (9201),A
    802B  HALT
    802C  LD A,EE           failure marker
    802E  LD (9201),A
    8031  HALT                                                          */

DECLARE @prog varbinary(max) =
    0x210090110091011000EDB03E0F06088010FD320092DD210093DD77053E00DD7E05FE3320073EAA07320192763EEE32019276;
DECLARE @src  varbinary(max) = 0x031425364758697A8B9CADBECFE0F102;

EXEC dbo.z80_load_bin @data = @prog, @addr = 32768;   -- 0x8000
EXEC dbo.z80_load_bin @data = @src,  @addr = 36864;   -- 0x9000

UPDATE dbo.z80_state SET r_PC = 32768, r_SP = 65024, iff1 = 0 WHERE id = 1;
GO

DECLARE @ts int, @instr int;
EXEC dbo.z80_run @budget = 5000, @assert_int = 0,
                 @out_ts = @ts OUTPUT, @out_instr = @instr OUTPUT;
PRINT CONCAT('executed ', @instr, ' instructions in ', @ts, ' T-states',
             CASE WHEN @instr = 48 THEN '  (48 = correct, stops at HALT)'
                  ELSE '  EXPECTED 48' END);
GO

/* ---- assertions ---------------------------------------------------- */
SELECT 'accumulator / DJNZ' AS test,
       CASE WHEN val = 0x33 THEN 'PASS' ELSE CONCAT('FAIL got ', val) END AS result
FROM dbo.z80_mem WHERE addr = 37376        -- 0x9200
UNION ALL
SELECT 'DD indexed + RLCA',
       CASE WHEN val = 0x55 THEN 'PASS'
            WHEN val = 0xEE THEN 'FAIL indexed addressing wrong'
            ELSE CONCAT('FAIL got ', val) END
FROM dbo.z80_mem WHERE addr = 37377        -- 0x9201
UNION ALL
SELECT 'LDIR block copy',
       CASE WHEN COUNT(*) = 16 THEN 'PASS'
            ELSE CONCAT('FAIL ', 16 - COUNT(*), ' bytes differ') END
FROM dbo.z80_mem s
JOIN dbo.z80_mem d ON d.addr = s.addr + 256 AND d.val = s.val
WHERE s.addr BETWEEN 36864 AND 36879;      -- 0x9000..0x900F
GO

SELECT r_A, r_F, r_B, r_H, r_L, r_D, r_E,
       r_IXH * 256 + r_IXL AS ix,
       r_PC AS pc, halt
FROM dbo.z80_state WHERE id = 1;
GO

/* ---- breakpoint check --------------------------------------------
   Re-run the same program but stop the moment PC reaches 0x8012, the
   LD ($9200),A that follows the DJNZ loop. A is 0x33 by then but not yet
   stored, so 0x9200 must still hold 0 while the register shows 0x33.
   Sampling PC between frames could never catch this.                  */
UPDATE dbo.z80_mem SET val = 0 WHERE addr IN (37376, 37377);
UPDATE dbo.z80_state SET r_PC = 32768, r_SP = 65024, iff1 = 0 WHERE id = 1;
GO

DECLARE @ts int, @instr int, @stopped bit, @pc int, @a int, @mem int;
EXEC dbo.z80_run @budget = 5000, @assert_int = 0, @stop_pc = 32786,
                 @out_ts = @ts OUTPUT, @out_instr = @instr OUTPUT,
                 @out_stopped = @stopped OUTPUT;
SELECT @pc = r_PC, @a = r_A FROM dbo.z80_state WHERE id = 1;
SELECT @mem = val FROM dbo.z80_mem WHERE addr = 37376;
PRINT CONCAT('breakpoint: ',
    CASE WHEN @stopped = 1 AND @pc = 32786 AND @a = 51 AND @mem = 0
         THEN 'PASS'
         ELSE CONCAT('FAIL stopped=', @stopped, ' pc=', @pc,
                     ' a=', @a, ' mem=', @mem) END,
    '  (', @instr, ' instructions, ', @ts, ' T-states)');
GO
