from ctypes import Union, LittleEndianStructure, c_uint16

class WordBitfield(Union):
	_anonymous_ = ('bits',)

	class bits(LittleEndianStructure):
		_fields_ = [
		]

	_fields_ = [
		('value', c_uint16),
		('bits', bits),
	]

	def _to_dict (self) -> dict:
		return dict((field, getattr(self, field)) for field, _, _ in self.bits._fields_ )

	def __str__ (self) -> str:
		return '{}(\n{}\n)'.format(
			type(self).__name__,
			', \n'.join([f'  {k}={v}' for k,v in self._to_dict().items()])
		)