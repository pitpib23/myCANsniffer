import serial
import time
# Open the simulator side of the bridge
ser = serial.Serial('COM2', 115200)
while True:
    ser.write(b"t123854415441\r") # Sends CAN ID 123, 4 bytes of data
    time.sleep(0.1)