import importlib.util,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtCore import QObject,pyqtSignal,pyqtSlot,pyqtProperty,Qt
from PyQt6.QtQml import QQmlExpression,QQmlEngine
from PyQt6 import sip
D=Path(__file__).resolve().parents[1]
class Controller(QObject):
    referenceBusyChanged=pyqtSignal()
    @pyqtProperty(bool,notify=referenceBusyChanged)
    def referenceBusy(self):return False
    @pyqtProperty(bool)
    def gain_phase_compensation(self):return False
    @pyqtSlot(result=str)
    def get_config_json(self):return json.dumps({'adc_decimation':4,'trigger':dict(mode='radar',vector_chunks=1,interval_chunks=96,offset_chunks=2,pulses=384,cycle_windows=3255,accumulation_chunks=16)})
    @pyqtSlot(str)
    def apply_config_json(self,text):self.last=json.loads(text)
class UITest(unittest.TestCase):
    def test_left_rx_radar_readback(self):
        spec=importlib.util.spec_from_file_location('radar_ui',D/'pulse-radar-analyzer-espargi.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        args=['radar','-s','unused']
        with patch.object(sys,'argv',args):app=m.EspargosPulseRadarAnalyzer(args)
        app.ready=True;app.initialize_qml(D/'pulse-radar-analyzer-ui-espargi.qml',{'iqcontrol':None})
        window=app.engine.rootObjects()[0]
        try:
            drawer=window.findChild(QObject,'radarRxDrawer')
            self.assertEqual(drawer.property('edge'),Qt.Edge.LeftEdge)
            self.assertFalse(window.property('receiverControlsOnRight'))
            settings=window.findChild(QObject,'iqCaptureSettings');controller=Controller()
            settings.setProperty('controller',controller)
            context=QQmlEngine.contextForObject(settings)
            expr=QQmlExpression(context,settings,'loadFromController(); JSON.stringify(collectConfig())')
            result,_=expr.evaluate();self.assertFalse(expr.hasError(),expr.error().toString())
            trigger=json.loads(result)['trigger']
            self.assertEqual(trigger,dict(mode='radar',vector_chunks=1,interval_chunks=96,offset_chunks=2,pulses=384,cycle_windows=3255,accumulation_chunks=16))
        finally:sip.delete(window);sip.delete(app.engine)
if __name__=='__main__':unittest.main()
