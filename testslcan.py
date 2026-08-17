import serial
import time
import math
import random
import struct

PORT = "COM2"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=1)

start_time = time.monotonic()

# ---------------------------------------------------------
# Simulated machine state
# ---------------------------------------------------------

rpm = 0
target_rpm = 1450

temperature = 27.0
motor_current = 0.0
voltage = 398.0

production_count = 12450

machine_running = False
alarm = False

last_send: dict[int, float] = {
    0x100: 0.0,
    0x180: 0.0,
    0x200: 0.0,
    0x280: 0.0,
    0x300: 0.0,
    0x380: 0.0,
    0x420: 0.0,
}


def send_can(can_id, data):
    """
    Send standard 11-bit CAN frame using SLCAN format.

    Example:
        ID 0x123
        Data 01 02 03 04

    becomes:

        t123401020304\\r
    """

    dlc = len(data)

    frame = (
        f"t{can_id:03X}"
        f"{dlc:X}"
        f"{data.hex().upper()}\r"
    )

    ser.write(frame.encode())


def due(can_id, interval):
    now = time.monotonic()

    if now - last_send[can_id] >= interval:
        last_send[can_id] = now
        return True

    return False


while True:

    now = time.monotonic()
    elapsed = now - start_time

    # =====================================================
    # MACHINE BEHAVIOUR
    # =====================================================

    # Machine starts after 3 seconds
    machine_running = elapsed > 3

    if machine_running:

        # Motor ramps toward 1450 RPM
        if rpm < target_rpm:
            rpm += random.randint(5, 15)

        rpm = min(rpm, target_rpm)

        # Small realistic RPM fluctuation
        rpm += random.randint(-3, 3)

        # Current depends roughly on motor load
        load = 0.65 + 0.10 * math.sin(elapsed / 5)

        motor_current = (
            2.0
            + load * 11
            + random.uniform(-0.15, 0.15)
        )

        # Motor slowly heats up
        target_temperature = 48 + load * 12

        temperature += (
            target_temperature - temperature
        ) * 0.002

        temperature += random.uniform(-0.02, 0.02)

    else:

        rpm = 0
        motor_current = 0

        temperature += (
            27 - temperature
        ) * 0.001

    # Three-phase voltage moves slowly
    voltage = (
        398
        + 2.5 * math.sin(elapsed / 10)
        + random.uniform(-0.3, 0.3)
    )

    # Rare simulated alarm condition
    alarm = temperature > 70


    # =====================================================
    # 0x100 - PLC HEARTBEAT / MACHINE STATE
    #
    # 10 ms
    #
    # Byte 0 : rolling counter
    # Byte 1 : machine state
    # Byte 2 : status flags
    # =====================================================

    if due(0x100, 0.010):

        counter = int(elapsed * 100) & 0xFF

        state = 2 if machine_running else 1

        flags = 0

        if machine_running:
            flags |= 0x01

        if alarm:
            flags |= 0x02

        data = bytes([
            counter,
            state,
            flags,
            0x00
        ])

        send_can(0x100, data)


    # =====================================================
    # 0x180 - MOTOR SPEED
    #
    # 20 ms
    #
    # Bytes 0-1 : RPM
    # Bytes 2-3 : target RPM
    # Byte 4     : motor state
    # =====================================================

    if due(0x180, 0.020):

        data = struct.pack(
            "<HHB",
            max(0, int(rpm)),
            target_rpm,
            1 if machine_running else 0
        )

        send_can(0x180, data)


    # =====================================================
    # 0x200 - MOTOR ELECTRICAL DATA
    #
    # 50 ms
    #
    # Bytes 0-1 : current ×100 A
    # Bytes 2-3 : voltage ×10 V
    # Bytes 4-5 : power ×10 W
    # =====================================================

    if due(0x200, 0.050):

        current_raw = int(motor_current * 100)
        voltage_raw = int(voltage * 10)

        power = voltage * motor_current * 0.85

        power_raw = int(power / 10)

        data = struct.pack(
            "<HHH",
            current_raw,
            voltage_raw,
            power_raw
        )

        send_can(0x200, data)


    # =====================================================
    # 0x280 - TEMPERATURE
    #
    # 100 ms
    #
    # Bytes 0-1 : motor temperature ×10 °C
    # Bytes 2-3 : ambient temperature ×10 °C
    # =====================================================

    if due(0x280, 0.100):

        ambient = (
            26.5
            + math.sin(elapsed / 20) * 0.5
        )

        data = struct.pack(
            "<HH",
            int(temperature * 10),
            int(ambient * 10)
        )

        send_can(0x280, data)


    # =====================================================
    # 0x300 - DIGITAL I/O
    #
    # 100 ms
    #
    # Byte 0:
    #
    # bit 0 = Start
    # bit 1 = Motor running
    # bit 2 = Product sensor
    # bit 3 = Safety door
    # bit 4 = Alarm
    #
    # Byte 1 = output flags
    # =====================================================

    if due(0x300, 0.100):

        inputs = 0

        if machine_running:
            inputs |= 1 << 0
            inputs |= 1 << 1

        # Product sensor pulses periodically
        if int(elapsed * 2) % 10 == 0:
            inputs |= 1 << 2

        # Safety door closed
        inputs |= 1 << 3

        if alarm:
            inputs |= 1 << 4

        outputs = 0

        if machine_running:
            outputs |= 1 << 0

        data = bytes([
            inputs,
            outputs
        ])

        send_can(0x300, data)


    # =====================================================
    # 0x380 - PRODUCTION COUNTER
    #
    # 500 ms
    #
    # Bytes 0-3 : total production count
    # =====================================================

    if due(0x380, 0.500):

        # Produce roughly one item every 2 seconds
        if machine_running and int(elapsed * 2) % 4 == 0:
            production_count += 1

        data = struct.pack(
            "<I",
            production_count
        )

        send_can(0x380, data)


    # =====================================================
    # 0x420 - DIAGNOSTIC / DEVICE INFORMATION
    #
    # 1 second
    #
    # Byte 0 : node ID
    # Byte 1 : firmware major
    # Byte 2 : firmware minor
    # Byte 3 : error code
    # Byte 4 : operating mode
    # =====================================================

    if due(0x420, 1.000):

        error_code = 1 if alarm else 0

        data = bytes([
            0x05,       # Node ID
            0x02,       # FW major
            0x07,       # FW minor
            error_code,
            0x01 if machine_running else 0x00
        ])

        send_can(0x420, data)


    time.sleep(0.001)