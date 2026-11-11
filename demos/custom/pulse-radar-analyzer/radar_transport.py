"""Process-isolated UDP reception for the custom radar analyzer."""
import multiprocessing
import queue
import socket
import time
from types import MethodType


def _capture(sock, output, stop, counters):
    # Batching avoids one multiprocessing message per tiny controller datagram.
    # Queue and batch sizes bound storage; overload is explicit, never silent.
    from espargos.sensor import SensorPacket, SensorMessageReassembler
    reassembler = SensorMessageReassembler()
    sock.settimeout(.005)
    batch = []
    deadline = time.monotonic() + .005
    try:
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(65535)
                counters[0] += 1
                counters[1] += len(data)
                if data:
                    if data[:4] == b'\xad\xfb\xca\xde':
                        try:
                            batch.extend(reassembler.push(SensorPacket.from_bytes(data)))
                        except ValueError:
                            # Let the existing dispatcher report malformed packets.
                            batch.append(data)
                    else:
                        batch.append(data)
            except socket.timeout:
                pass
            if batch and (len(batch) >= 64 or time.monotonic() >= deadline):
                try:
                    output.put(batch, timeout=.2)
                except queue.Full:
                    counters[2] += len(batch)
                    stop.set()
                    break
                batch = []
                deadline = time.monotonic() + .005
    except OSError:
        if not stop.is_set():
            counters[3] += 1
            stop.set()
    finally:
        # Shutdown must not wait on a parent that has stopped consuming.
        output.cancel_join_thread()
        sock.close()


def _buffered_udp_loop(board):
    context = multiprocessing.get_context('spawn')
    output = context.Queue(maxsize=256)
    stop = context.Event()
    counters = context.Array('Q', 4, lock=False)
    board.radar_transport_counters = counters
    process = context.Process(target=_capture, args=(board._udp_sock, output, stop, counters),
                              name='radar-udp-'+board.host, daemon=True)
    try:
        process.start()
        while board._stream_connected:
            try:
                messages = output.get(timeout=.2)
            except queue.Empty:
                if stop.is_set() or not process.is_alive():
                    board._logger.error('Radar UDP receiver stopped or its bounded buffer overflowed')
                    board._stream_connected = False
                    break
                continue
            for message in messages:
                if not board._enqueue_stream_message(message):
                    return
    finally:
        stop.set()
        if process.pid is not None:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)
        output.close()
        output.cancel_join_thread()



def _dispatch(board, message):
    from espargos.sensor import SensorMessage, get_sensor_message_type_header
    if not isinstance(message, SensorMessage):
        return board._radar_original_stream_handler(message)
    try:
        kind = get_sensor_message_type_header(message)
    except ValueError:
        board._logger.debug('Ignoring sensor message without a logical type header')
        return
    with board._sensor_message_subscriptions_lock:
        subscriptions = tuple(board._sensor_message_subscriptions.get(kind, ()))
    for subscription in subscriptions:
        try:
            subscription.callback(message)
        except Exception:
            board._logger.exception('Sensor-message callback failed for type 0x%08x', kind)


def install_radar_transport(pool):
    """Install before Pool.open(); no shared Board code or RF settings change."""
    from espargos.board import Board
    # Pool materializes boards during open(). Supply configured instances first.
    if getattr(pool, '_opened', False):
        raise RuntimeError('Install radar transport before opening the pool')
    for host in pool.hosts:
        if host not in pool._provided:
            pool._provided[host] = Board(host)
    for board in pool._provided.values():
        if getattr(board, '_stream_connected', False):
            raise RuntimeError('Install radar transport before opening the pool')
        if not hasattr(board, '_radar_original_stream_handler'):
            board._radar_original_stream_handler = board._stream_handle_message
            board._stream_handle_message = MethodType(_dispatch, board)
        board._stream_loop_udp = MethodType(_buffered_udp_loop, board)
