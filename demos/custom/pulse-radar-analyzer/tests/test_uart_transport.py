"""UART merge regressions without opening a serial device."""
import unittest
from unittest.mock import Mock, patch

from espargos.uart import UARTClient, UARTTimeoutError, FRAME_TYPE_SENSOR_DATA, FRAME_TYPE_LOG


class UARTTransportTest(unittest.TestCase):
    def setUp(self):
        with patch('espargos.uart.serial.tools.list_ports.comports', return_value=[]):
            self.client = UARTClient('uart:/dev/unused')

    def test_reconnect_discards_stale_receive_data(self):
        client = self.client
        client._rx_buffer.extend(b'stale partial frame')
        with patch.object(client, '_open_serial'), patch.object(client, '_start_keepalive_thread'), \
                patch.object(client, 'hello', side_effect=lambda **kw: self.assertFalse(client._rx_buffer)):
            client.connect()
        self.assertTrue(client._connected)

    def test_failed_activation_releases_port_and_allows_retry(self):
        client = self.client
        port = Mock()
        with patch.object(client, '_open_serial', return_value=port), \
                patch.object(client, 'hello', side_effect=UARTTimeoutError('no hello')), \
                patch.object(client, '_activate_transport_mode', side_effect=UARTTimeoutError('activation failed')), \
                patch.object(client, 'disable_sensor_stream'), patch.object(client, 'disable_transport_mode'):
            with self.assertRaisesRegex(UARTTimeoutError, 'activation failed'):
                client.connect()
        port.close.assert_called_once()
        self.assertIsNone(client._serial)
        self.assertFalse(client._connected)
        with patch.object(client, '_open_serial'), patch.object(client, 'hello'), \
                patch.object(client, '_start_keepalive_thread'):
            client.connect()
        self.assertTrue(client._connected)
        self.assertFalse(client._stop_event.is_set())

    def test_activation_discards_boot_bytes_before_hello(self):
        client = self.client
        client._serial = Mock()
        client._rx_buffer.extend(b'boot text')
        with patch('espargos.uart.time.sleep'), \
                patch.object(client, 'hello', side_effect=lambda: self.assertFalse(client._rx_buffer)):
            client._activate_transport_mode()
        self.assertEqual(client._serial.baudrate, client.baudrate)

    def test_bad_callbacks_do_not_block_other_subscribers_or_later_frames(self):
        for frame, subscribe, payload, expected in (
            (FRAME_TYPE_SENSOR_DATA, self.client.add_stream_callback, b'IQ data', b'IQ data'),
            (FRAME_TYPE_LOG, self.client.add_log_callback, b'log \xff', 'log \ufffd'),
        ):
            with self.subTest(frame=frame):
                failing, receiving = Mock(side_effect=ValueError('subscriber failed')), Mock()
                subscribe(failing)
                subscribe(receiving)
                with self.assertLogs('pyespargos.uart', level='ERROR'):
                    self.client._handle_frame(frame, 0, payload)
                    self.client._handle_frame(frame, 0, payload)
                self.assertEqual(receiving.call_count, 2)
                receiving.assert_called_with(expected)


if __name__ == '__main__':
    unittest.main()
