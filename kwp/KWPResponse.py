class KWPResponse:
	frame = False
	status = False
	data = False

	def set_frame (self, frame):
		self.frame = frame 
		return self 

	def set_status (self, status):
		self.status = status
		return self 

	def set_data (self, data):
		self.data = data
		return self

	def get_frame (self):
		return self.frame

	def get_status (self):
		return self.status

	def get_data (self):
		return self.data