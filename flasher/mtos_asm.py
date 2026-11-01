"""
MTOS payload tooling for Siemens SIMK43 (Infineon C167) ECUs.

MTOS ("MT" OS) is a stock Siemens diagnostic feature that lets a tester upload a
short routine into RAM at 0xC000 and execute it via KWP2000
StartRoutineByLocalIdentifier(0xFA). This module provides:

  * crc16_mtos()  - the exact checksum the firmware validates the blob with
  * build_blob()  - wrap a body of machine code in the MTOS header the
                    firmware expects (signature, 'MT' magic, CRC, CRC range)
  * Asm           - a tiny C167 assembler (only the opcodes we need, every one
                    taken from verified ca663056 firmware disassembly)
  * ready-made payload/loader generators

MTOS execution contract (reverse engineered from ca663056, validator @0x003460):

  Blob is uploaded to RAM starting at 0xC000. Header (little-endian):

    0xC000  u16  stored CRC16 of the body (seed 0x544D, poly 0xA001 reflected)
    0xC002  u16  unused on the 0xFA path
    0xC004  u32  CRC range START, 24-bit linear (offset in low word, segment hi)
    0xC008  u32  CRC range END, inclusive, same encoding
    0xC00C  6    signature - must equal the firmware's flash bytes at 0x3FE8
    0xC012  u8   'M' (0x4D)
    0xC013  u8   'T' (0x54)
    0xC014  ...  entry point; firmware does a far `calls 0xC014`

  The payload is entered at 0xC014 via an inter-segment call and MUST terminate
  with a single `rets`. While it runs the firmware is blocked, so the payload
  has exclusive use of the ASC0 UART (= the K-line).

  Entry rules:
    * terminate with rets (Asm.rets())
    * DPP0 is left at 0x24 by the validator; DPP3 stays 3. Use the 0xC000-0xFFFF
      window and SFRs directly; use EXTP for anything else.
    * preserve r0 (stack pointer); save/restore GPRs you clobber
    * SFRs are always reachable (DPP3==3 maps 0xFE00-0xFFFF to itself)

  The signature at 0x3FE8 is firmware-version specific, so callers should read it
  live from the ECU (ReadMemoryByAddress 0x3FE8, 6 bytes) rather than hardcoding.
"""

import struct

MTOS_BASE = 0xC000  # where the blob is uploaded
MTOS_ENTRY = 0xC014  # firmware entry point (calls 0xC014)
MTOS_HEADER_LEN = MTOS_ENTRY - MTOS_BASE  # 0x14
MTOS_SIGNATURE_ADDRESS = 0x3FE8  # flash location of the 6-byte reference signature
MTOS_SIGNATURE_LENGTH = 6
CRC_SEED = 0x544D  # the 'MT' word; firmware seeds the CRC with it
CRC_POLY = 0xA001

# ASC0 (K-line) SFRs
S0TBUF = 0xFEB0
S0RBUF = 0xFEB2
S0TIC = 0xFF6C
S0RIC = 0xFF6E

# A convenient, known-executable scratch area for a second stage (XRAM 0xE000-0xEEFF)
STAGE2_ADDRESS = 0xE000


def crc16_mtos(data: bytes, seed: int = CRC_SEED) -> int:
	"""Reproduce the firmware CRC routine at 0x000E32 byte-for-byte.

	Per byte: for each of 8 bits, test (crc ^ (byte>>i)) & 1; if set,
	crc = (crc>>1) ^ 0xA001, else crc >>= 1.
	"""
	crc = seed & 0xFFFF
	for byte in data:
		bb = byte & 0xFF
		for _ in range(8):
			if (crc ^ bb) & 1:
				crc = (crc >> 1) ^ CRC_POLY
			else:
				crc = crc >> 1
			crc &= 0xFFFF
			bb >>= 1
	return crc & 0xFFFF


def build_blob(body: bytes, signature: bytes,
               crc_start: int = MTOS_ENTRY, segment: int = 0) -> bytes:
	"""Wrap `body` (machine code starting at 0xC014) in the MTOS header.

	`signature` must be the 6 bytes read from the target ECU at 0x3FE8.
	"""
	if len(signature) != MTOS_SIGNATURE_LENGTH:
		raise ValueError('signature must be %d bytes (read live from 0x%X)'
		                 % (MTOS_SIGNATURE_LENGTH, MTOS_SIGNATURE_ADDRESS))
	if not body:
		raise ValueError('empty body')

	crc_end = crc_start + len(body) - 1
	crc = crc16_mtos(body)

	header = bytearray(MTOS_HEADER_LEN)
	struct.pack_into('<H', header, 0x00, crc)                      # 0xC000
	struct.pack_into('<H', header, 0x02, 0x0000)                   # 0xC002 unused
	struct.pack_into('<I', header, 0x04, (segment << 16) | crc_start)  # 0xC004
	struct.pack_into('<I', header, 0x08, (segment << 16) | crc_end)    # 0xC008
	header[0x0C:0x12] = signature                                  # 0xC00C..0xC011
	header[0x12] = 0x4D  # 'M'
	header[0x13] = 0x54  # 'T'
	return bytes(header) + bytes(body)


# ---------------------------------------------------------------------------
# Tiny C167 assembler. Only the instructions we need; every encoding is
# annotated with the ca663056 address it was verified against.
# ---------------------------------------------------------------------------

def _rw(reg):  # word register field for e6/f2/f6: 0xF0 | n
	return 0xF0 | (reg & 0x0F)


def _rb_code(name):
	"""Byte-register code: RL0=0, RH0=1, RL1=2, RH1=3, ... RL7=14, RH7=15."""
	num = int(name[2:])
	return num * 2 + (0 if name[1] == 'L' else 1)


def _rbf(name):  # byte-register field for f3/f7/47/e7-reg: 0xF0 | code
	return 0xF0 | _rb_code(name)


def _bitoff(addr):
	"""Bit-offset byte used by JB/JNB/BSET/BCLR/BFLDL."""
	if 0xFD00 <= addr <= 0xFDFF:
		return (addr - 0xFD00) // 2            # bit-addressable RAM: 0x00..0x7F
	if 0xFF00 <= addr <= 0xFFDF:
		return 0x80 + (addr - 0xFF00) // 2     # SFR: 0x80..0xEF
	raise ValueError('address 0x%X is not bit-addressable' % addr)


def _sfr_short(addr):
	"""Short reg address for SFRs (0xFE00-0xFFFF), used by e7 immediate form."""
	if not (0xFE00 <= addr <= 0xFFFF):
		raise ValueError('0x%X not an SFR' % addr)
	return (addr - 0xFE00) // 2


class Asm:
	"""Two-pass assembler. All instructions are fixed size, so labels resolve
	trivially. Jump targets are given as label names."""

	def __init__(self):
		self._items = []      # list of dicts: {'size', 'bytes'|'jump'}
		self._labels = {}     # name -> item index

	# -- label / emit helpers --
	def label(self, name):
		if name in self._labels:
			raise ValueError('duplicate label %r' % name)
		self._labels[name] = len(self._items)
		return self

	def _emit(self, *bytes_):
		self._items.append({'size': len(bytes_), 'bytes': bytes(bytes_), 'jump': None})
		return self

	def _emit_jump(self, opbytes, target, size, kind):
		self._items.append({'size': size, 'bytes': None,
		                    'jump': {'op': bytes(opbytes), 'target': target, 'kind': kind}})
		return self

	# -- data moves (verified: e6 @0a2208-ish, f0 @0e0f04, f2 @0a2c50, f6 @0ac322) --
	def mov_imm(self, reg, imm16):            # mov Rw,#imm16   e6 Fn lo hi
		return self._emit(0xE6, _rw(reg), imm16 & 0xFF, (imm16 >> 8) & 0xFF)

	def mov_rr(self, dst, src):               # mov Rw,Rw       f0 nm
		return self._emit(0xF0, ((dst & 0xF) << 4) | (src & 0xF))

	def mov_mem_reg(self, addr, reg):         # mov mem16,Rw    f6 Fn lo hi
		return self._emit(0xF6, _rw(reg), addr & 0xFF, (addr >> 8) & 0xFF)

	def mov_reg_mem(self, reg, addr):         # mov Rw,mem16    f2 Fn lo hi
		return self._emit(0xF2, _rw(reg), addr & 0xFF, (addr >> 8) & 0xFF)

	def movb_mem_rb(self, addr, rb):          # movb mem16,RLn  f7 Fn lo hi (@0a9214)
		return self._emit(0xF7, _rbf(rb), addr & 0xFF, (addr >> 8) & 0xFF)

	def movb_rb_mem(self, rb, addr):          # movb RLn,mem16  f3 Fn lo hi (@0a9300)
		return self._emit(0xF3, _rbf(rb), addr & 0xFF, (addr >> 8) & 0xFF)

	def movb_mem_imm(self, addr, imm8):       # movb SFR,#imm8  e7 short imm 00 (@0a91f0)
		return self._emit(0xE7, _sfr_short(addr), imm8 & 0xFF, 0x00)

	def movb_ind_rb(self, ptr_reg, rb):       # movb [Rw],RLn   b9 (code<<4|rw) (@0a2bda)
		return self._emit(0xB9, (_rb_code(rb) << 4) | (ptr_reg & 0xF))

	def movb_rb_ind(self, rb, ptr_reg):       # movb RLn,[Rw]   a9 (code<<4|rw) (@0e0f36)
		return self._emit(0xA9, (_rb_code(rb) << 4) | (ptr_reg & 0xF))

	def movbz(self, dst, rb):                 # movbz Rw,RLn    c0 (code<<4|rw) (@0e0f54)
		return self._emit(0xC0, (_rb_code(rb) << 4) | (dst & 0xF))

	# -- arithmetic (verified: 08 @0a2bd8, 28 @0a2c4e, 00 @0e29b0, 5c @0e0f6a) --
	def add_imm(self, reg, imm16):            # add Rw,#imm16   06 Fn lo hi (@0a91c4)
		return self._emit(0x06, _rw(reg), imm16 & 0xFF, (imm16 >> 8) & 0xFF)

	def add_short(self, reg, data3):          # add Rw,#data3   08 (reg<<4|data) (@0a2bd8)
		return self._emit(0x08, ((reg & 0xF) << 4) | (data3 & 0x7))

	def add_rr(self, dst, src):               # add Rw,Rw       00 (dst<<4|src) (@0e29b0)
		return self._emit(0x00, ((dst & 0xF) << 4) | (src & 0xF))

	def sub_short(self, reg, data3):          # sub Rw,#data3   28 (reg<<4|data) (@0a2c4e)
		return self._emit(0x28, ((reg & 0xF) << 4) | (data3 & 0x7))

	def shl_imm(self, reg, count):            # shl Rw,#data4   5c (count<<4|reg) (@0e0f6a)
		return self._emit(0x5C, ((count & 0xF) << 4) | (reg & 0xF))

	# -- bit ops on SFRs (verified: bclr 6e b7 @0037f4, bset 7f e6 @0ac2d2) --
	def bset(self, addr, bit):                # bset addr.bit   (bit<<4|0xF) bitoff
		return self._emit(((bit & 0xF) << 4) | 0x0F, _bitoff(addr))

	def bclr(self, addr, bit):                # bclr addr.bit   (bit<<4|0xE) bitoff
		return self._emit(((bit & 0xF) << 4) | 0x0E, _bitoff(addr))

	# -- control flow --
	def rets(self):                           # rets            db 00 (@0e0ffc)
		return self._emit(0xDB, 0x00)

	def nop(self):                            # nop             cc 00
		return self._emit(0xCC, 0x00)

	def db(self, *byte_values):               # raw bytes escape hatch
		"""Emit raw machine-code bytes verbatim, for opcodes this assembler does
		not model. You are responsible for their correctness and (even) size."""
		return self._emit(*[b & 0xFF for b in byte_values])

	def srvwdt(self):                         # srvwdt          a7 58 a7 a7 (@000634,0006c2,0008e2,000a58)
		# Protected 4-byte instruction that reloads the watchdog timer. The
		# firmware is blocked while our payload runs, so nothing else services
		# the watchdog; any loop that waits for host bytes MUST call this or the
		# C167 resets mid-payload (verified: long polls went silent until this
		# was sprinkled into the wait loop).
		return self._emit(0xA7, 0x58, 0xA7, 0xA7)

	def extp(self, page, rng=1):              # extp #pag,#rng  d7 (0x40|(rng-1)<<4) lo hi (@0a2c42)
		return self._emit(0xD7, 0x40 | ((rng - 1) << 4), page & 0xFF, (page >> 8) & 0xFF)

	def jmpi(self, ptr_reg):                  # jmpi cc_UC,[Rw] 9c (0|reg) (@0a91ee)
		return self._emit(0x9C, ptr_reg & 0x0F)

	def calli(self, ptr_reg):                 # calli cc_UC,[Rw] ab (0|reg) (std C166 opcode)
		return self._emit(0xAB, ptr_reg & 0x0F)

	def calls(self, segment, addr):           # calls seg,addr  da ss lo hi (@0a2bec)
		return self._emit(0xDA, segment & 0xFF, addr & 0xFF, (addr >> 8) & 0xFF)

	# jmpr cc,rel8  ->  (cc<<4|0xD) rel   (@0034a0: 8d ee for cc_C)
	_CC = {'UC': 0x0, 'Z': 0x2, 'EQ': 0x2, 'NZ': 0x3, 'NE': 0x3,
	       'C': 0x8, 'ULT': 0x8, 'NC': 0x9, 'UGE': 0x9}

	def jmpr(self, cc, target):
		op = ((self._CC[cc] & 0xF) << 4) | 0x0D
		return self._emit_jump([op], target, 2, 'rel8')

	def jb(self, addr, bit, target):          # jb addr.bit,rel  8a bitoff rel (bit<<4) (@0a91dc)
		return self._emit_jump([0x8A, _bitoff(addr), (bit & 0xF) << 4], target, 4, 'jb')

	def jnb(self, addr, bit, target):         # jnb addr.bit,rel 9a bitoff rel (bit<<4) (@001988)
		return self._emit_jump([0x9A, _bitoff(addr), (bit & 0xF) << 4], target, 4, 'jb')

	# -- convenience macros --
	def kline_send_reg(self, rb):
		"""Transmit the byte in `rb` over the K-line (ASC0), polled."""
		lbl = '__tx%d' % len(self._items)
		self.movb_mem_rb(S0TBUF, rb)
		self.label(lbl)
		self.jnb(S0TIC, 7, lbl)   # wait until TIR set (buffer -> shift register)
		self.bclr(S0TIC, 7)       # clear for next byte
		return self

	def kline_send_imm(self, imm8):
		lbl = '__txi%d' % len(self._items)
		self.movb_mem_imm(S0TBUF, imm8)
		self.label(lbl)
		self.jnb(S0TIC, 7, lbl)
		self.bclr(S0TIC, 7)
		return self

	def kline_recv_reg(self, rb):
		"""Receive one byte from the K-line (ASC0) into `rb`, polled.

		The wait loop services the watchdog every iteration, so it is safe to
		block here for an arbitrarily long time waiting for the host - which is
		exactly what a loader must do. Without srvwdt the C167 watchdog resets
		the ECU mid-payload.
		"""
		lbl = '__rx%d' % len(self._items)
		self.label(lbl)
		self.srvwdt()             # keep the watchdog happy while we wait
		self.jnb(S0RIC, 7, lbl)   # wait until RIR set (byte received)
		self.movb_rb_mem(rb, S0RBUF)
		self.bclr(S0RIC, 7)
		return self

	def kline_flush(self):
		"""Make sure the last transmitted byte has fully left the shift register
		before returning to the firmware.

		S0TIC.7 (TIR) only signals that S0TBUF was copied into the shift
		register, not that transmission finished. A payload that `rets` straight
		after its last kline_send loses that last byte, because the firmware
		takeover clobbers the still-shifting byte. Sending one dummy byte and
		waiting for its TIR guarantees the previous (real) byte has been shifted
		out completely; the dummy itself is expendable.
		"""
		return self.kline_send_imm(0x00)

	# -- assemble --
	def assemble(self, base=MTOS_ENTRY):
		offsets, off = [], 0
		for it in self._items:
			offsets.append(off)
			off += it['size']
		out = bytearray()
		for idx, it in enumerate(self._items):
			if it['bytes'] is not None:
				out += it['bytes']
				continue
			j = it['jump']
			tgt_idx = self._labels[j['target']]
			tgt_off = offsets[tgt_idx]
			self_off = offsets[idx]
			rel = (tgt_off - (self_off + it['size'])) // 2
			if not -128 <= rel <= 127:
				raise ValueError('jump out of rel8 range to %r' % j['target'])
			rel &= 0xFF
			if j['kind'] == 'rel8':            # op rel
				out += bytes([j['op'][0], rel])
			else:                               # jb/jnb: op bitoff rel (bit<<4)
				out += bytes([j['op'][0], j['op'][1], rel, j['op'][2]])
		return bytes(out)


# ---------------------------------------------------------------------------
# Ready-made payloads. Each returns the body (code from 0xC014); wrap with
# build_blob(body, signature).
# ---------------------------------------------------------------------------

def payload_marker(marker: int = 0xBEEF, address: int = 0xC002) -> bytes:
	"""Proof-of-execution with no I/O: write `marker` to RAM and return.

	Verify by reading `address` back with ReadMemoryByAddress; it changes from
	the uploaded header value to `marker`.
	"""
	a = Asm()
	a.mov_imm(4, marker)
	a.mov_mem_reg(address, 4)   # 0xC002 reaches via DPP3==3
	a.rets()
	return a.assemble()


def payload_kline_hello(message: bytes = b'MTOS') -> bytes:
	"""Proof-of-I/O: send `message` over the K-line, then return."""
	a = Asm()
	a.bclr(S0TIC, 6)            # disable firmware ASC0 TX interrupt while we poll
	for byte in message:
		a.kline_send_imm(byte)
	a.kline_flush()            # let the last real byte finish before we rets
	a.bset(S0TIC, 6)           # restore
	a.rets()
	return a.assemble()


def payload_memory_dump(address: int, length: int) -> bytes:
	"""Sample larger payload: stream `length` bytes starting at 24-bit linear
	`address` out of the K-line, then return."""
	if length <= 0 or length > 0xFFFF:
		raise ValueError('length must be 1..0xFFFF')
	page = address >> 14
	offset = address & 0x3FFF
	if offset + length > 0x4000:
		raise ValueError('dump must stay within one 16KB page for this sample')

	a = Asm()
	a.bclr(S0TIC, 6)           # own the TX path; leave global interrupts (watchdog) alone
	a.mov_imm(4, offset)       # r4 = read pointer within page
	a.mov_imm(5, length)       # r5 = remaining
	a.label('loop')
	a.srvwdt()                 # service the watchdog each byte so long dumps don't reset the ECU
	a.extp(page, 1)            # next access uses this page
	a.movb_rb_ind('RL0', 4)    # RL0 = [r4]
	a.kline_send_reg('RL0')
	a.add_short(4, 1)
	a.sub_short(5, 1)
	a.jmpr('NE', 'loop')
	a.kline_flush()            # ensure the final byte is fully shifted out before returning
	a.bset(S0TIC, 6)
	a.rets()
	return a.assemble()


def payload_loader(stage2_address: int = STAGE2_ADDRESS) -> bytes:
	"""Small K-line loader (stage 1).

	MTOS only gives us a tiny RAM window at 0xC000, so instead of uploading a
	large payload through the slow WriteMemoryByAddress path we upload this
	loader once and then stream the real ("stage 2") code to it over the
	K-line at full ASC0 speed.

	Protocol (all on the K-line, after StartRoutine(0xFA) launches the loader):

	    host -> ecu : u16 length, little-endian
	    host -> ecu : <length> bytes of stage-2 machine code
	    ecu         : runs the stage (far call), which may talk on the K-line
	    ecu  -> host : nothing added by the loader itself

	The loader copies the stream to `stage2_address`, far-calls it (so the stage
	returns to the loader with `rets`), then returns to the firmware.

	`stage2_address` must be writable RAM reachable through DPP3 (0xC000-0xFBFF);
	XRAM 0xE000 is the verified choice. The second stage must end with `rets`.

	Every receive services the watchdog, so the loader can wait indefinitely
	between bytes without the C167 resetting.
	"""
	if not (0xC000 <= stage2_address <= 0xFBFF):
		raise ValueError('stage2 must sit in the DPP3 window (0xC000-0xFBFF)')

	a = Asm()
	a.bclr(S0RIC, 6)           # divert incoming K-line bytes from the KWP stack to us
	a.bclr(S0TIC, 6)           # own the TX path too (stage 2 may transmit)
	# drop any byte the pre-hook left pending so the first recv is a real one
	a.movb_rb_mem('RL0', S0RBUF)
	a.bclr(S0RIC, 7)
	# count = low | (high << 8)  -> r6
	a.kline_recv_reg('RL0')
	a.movbz(6, 'RL0')
	a.kline_recv_reg('RL0')
	a.movbz(7, 'RL0')
	a.shl_imm(7, 8)
	a.add_rr(6, 7)             # r6 = total count
	a.mov_imm(4, stage2_address)  # r4 = destination pointer
	a.label('rx')
	a.kline_recv_reg('RL0')
	a.movb_ind_rb(4, 'RL0')    # [r4] = RL0
	a.add_short(4, 1)
	a.sub_short(6, 1)
	a.jmpr('NE', 'rx')
	a.calls(0, stage2_address)  # run the second stage (far call); it rets back here
	a.bset(S0RIC, 6)           # restore firmware interrupts
	a.bset(S0TIC, 6)
	a.rets()
	return a.assemble()


def payload_stage2_hello(message: bytes = b'OK') -> bytes:
	"""Sample stage-2 payload for the loader: frame `message` on the K-line.

	Emits AA 55 C2 <message> so the host can spot it unambiguously, then rets
	back to the loader. Because it only touches ASC0 SFRs and uses relative
	jumps, it is position independent and runs correctly at `STAGE2_ADDRESS`.
	"""
	a = Asm()
	for byte in (0xAA, 0x55, 0xC2):
		a.kline_send_imm(byte)
	for byte in message:
		a.kline_send_imm(byte)
	a.rets()
	return a.assemble()


if __name__ == '__main__':
	# Offline self-tests (no hardware).
	assert payload_marker() == bytes.fromhex('e6f4efbe' 'f6f402c0' 'db00'), \
		payload_marker().hex()
	# CRC sanity: seed with 'MT', empty-ish vectors
	assert crc16_mtos(b'') == CRC_SEED
	known = crc16_mtos(b'\x01\x02\x03\x04')
	assert 0 <= known <= 0xFFFF
	sig = b'111663'
	blob = build_blob(payload_marker(), sig)
	assert blob[0x12] == 0x4D and blob[0x13] == 0x54
	assert blob[0x0C:0x12] == sig
	# stored CRC must equal CRC over the body
	body = payload_marker()
	assert struct.unpack_from('<H', blob, 0)[0] == crc16_mtos(body)
	# CRC range start/end
	assert struct.unpack_from('<I', blob, 4)[0] == MTOS_ENTRY
	assert struct.unpack_from('<I', blob, 8)[0] == MTOS_ENTRY + len(body) - 1
	print('marker   :', payload_marker().hex())
	print('hello    :', payload_kline_hello(b'MTOS').hex())
	print('dump 0x40:', payload_memory_dump(0x000000, 0x40).hex())
	print('loader   :', payload_loader().hex())
	print('blob     :', blob.hex())
	print('all self-tests passed')
