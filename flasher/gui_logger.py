import xml.etree.ElementTree as ET
from PyQt5.QtWidgets import QTabWidget, QWidget
from types import NoneType

class ADXLoggerSingleton(type):
    _instances = {}
    def __new__(class_, *args, **kwargs):
        if class_ not in class_._instances:
            class_._instances[class_] = super(ADXLoggerSingleton, class_).__new__(class_, *args, **kwargs)
        return class_._instances[class_]

    def getInstance(self):
        if self.__class__ not in self._instances:
            raise Exception(f"Instance of {self.__class__} not created yet. Please create an instance first.")

        return self._instances[self.__class__]
    def setInstance(self, instance):
        self._instances[self.__class__] = instance

class ADXLogger(metaclass=ADXLoggerSingleton):
    def __init__(self, filename: str):
        self._tree = self._read_adx_file(filename)
        self._lookupTables = self._read_lookup_tables()
        self._values = self._read_values()
        self._histograms = self._read_histogram()
        self._monitors = self._read_monitors()
        __class__.setInstance(self)

    def get_current():
        return __class__.getInstance()

    def _read_adx_file(self, filename: str):
        tree = ET.parse(filename)
        root = tree.getroot()
        print(f"root: {root}")
        return tree
    def _read_lookup_tables(self):
        root = self._tree.getroot()
        lookup_tables = []
        for table in root.findall('.//ADXLOOKUPTABLE'):
            title = table.get('title')
            lookup_tables.append({
                'title': title.replace(" ", "_"),
                'table': table
                })
        return lookup_tables
    def _read_values(self):
        root = self._tree.getroot()
        values = []
        for value in root.findall('.//ADXVALUE'):
            title = value.get('title')
            datatype = value.find('datatype').text if type(value.find('datatype')) is not NoneType else "unknown"
            
            values.append({
                'title': title.replace(" ", "_"),
                'datatype': datatype,
                'value': value
            })
        
        return values
    def _read_histogram(self):
        root = self._tree.getroot()
        histograms = []
        for histogram in root.findall('.//ADXHISTOGRAM'):
            title = histogram.get('title')
            histograms.append({
                'title': title.replace(" ", "_"),
                'histogram': histogram
                })
        return histograms
    def _read_monitors(self):
        root = self._tree.getroot()
        monitors = []
        for monitor in root.findall('.//ADXMONITOR'):
            title = monitor.get('title')
            monitors.append({
                'title': title.replace(" ", "_"),
                'monitor': monitor
                })
        return monitors
    
    def init_tabs(self, tab_widget: QTabWidget):
        for table in self._histograms:
            widget = QWidget()
            widget.setObjectName(table['title'])
            tab_widget.addTab(widget, table['title'].replace("_", " "))



