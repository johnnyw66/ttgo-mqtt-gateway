import asyncio

import busio
import board
import digitalio

import time

#import paho.mqtt.client as mqtt
import adafruit_minimqtt.adafruit_minimqtt as mqtt
import os
import ssl
import ipaddress
import wifi
import socketpool
import adafruit_requests
import microcontroller


import adafruit_displayio_ssd1306
import displayio
from adafruit_display_text import label
import terminalio
import json
count_area = None
message_area = None
TEXT_OFFSET = 10
DEVICE_NAME = "DonglePICO"

#i2c = board.STEMMA_I2C()  # For using the built-in STEMMA QT connector on a microcontroller
I2C_SDA = board.IO18  # GPIO 18
I2C_SCL = board.IO19  # GPIO 19
try:
    # Initialize I2C
    i2c = busio.I2C(I2C_SCL, I2C_SDA)

    display_bus = displayio.I2CDisplay(i2c, device_address=0x3c)
    display = adafruit_displayio_ssd1306.SSD1306(display_bus, width=128, height=64)

    WIDTH = 128
    HEIGHT = 64  # Change to 64 if needed
    BORDER = 2

    # Make the display context
    splash = displayio.Group()
    display.root_group = splash

    color_bitmap = displayio.Bitmap(WIDTH, HEIGHT, 1)
    color_palette = displayio.Palette(1)
    color_palette[0] = 0xFFFFFF  # White

    bg_sprite = displayio.TileGrid(color_bitmap, pixel_shader=color_palette, x=0, y=0)
    splash.append(bg_sprite)

    # Draw a smaller inner rectangle
    inner_bitmap = displayio.Bitmap(WIDTH - BORDER * 2, HEIGHT - BORDER * 2, 1)
    inner_palette = displayio.Palette(1)
    inner_palette[0] = 0x000000  # Black
    inner_sprite = displayio.TileGrid(
        inner_bitmap, pixel_shader=inner_palette, x=BORDER, y=BORDER
    )
    splash.append(inner_sprite)

    # Draw a label
    text_area = label.Label(
        terminalio.FONT, text=DEVICE_NAME, color=0xFFFFFF, x=4, y=8
    )
    splash.append(text_area)

    count_area = label.Label(terminalio.FONT, text="", color=0xFFFFFF, x=70, y=8)
    splash.append(count_area)

    message_area = label.Label(terminalio.FONT, text="This is random message", color=0xFFFFFF, x=TEXT_OFFSET, y=HEIGHT // 2 - 1)
    splash.append(message_area)

except Exception as e:
    pass
    # No OLED
    
def set_message_area(text):
    if ('message_area' in globals()):
        message_area.text = text
        
# LILY Board Pins 
MODEM_POWER_PIN = board.IO23
MODEM_RST = board.IO5
MODEM_PWRKEY_PIN = board.IO4
MODEM_TX = board.IO27
MODEM_RX = board.IO26

POLL_SMS = 2

def isalnum(char):
    char_code = ord(char)
    safe_chars = "-_.~"  # Characters that don't need encoding
    return (48 <= char_code <= 57) or (65 <= char_code <= 90) or (97 <= char_code <= 122) or (char in safe_chars)

    
def url_encode(s):
    hex_map = "0123456789ABCDEF"
    encoded = ""
    for char in s:
        if isalnum(char) or char in "-_.~":
            encoded += char
        else:
            encoded += "%" + hex_map[ord(char) >> 4] + hex_map[ord(char) & 0xF]
    return encoded

quotes_url = "https://www.adafruit.com/api/quotes.php"

class GSMModule:
    def __init__(self, uart, pool, mqtt_broker='demo.mqtt.com', mqtt_port=1883, mqtt_user='mqtt_user', mqtt_password='mqtt_password', check_interval=60, on_disconnect=None, max_mqtt_retries=5):
        self.uart = uart
        self.lock = asyncio.Lock()
        self.response_buffer= []
        self.response_event = asyncio.Event()
        
        self.mqtt_client = mqtt.MQTT(broker=mqtt_broker, port=mqtt_port, username=mqtt_user, password=mqtt_password, socket_pool=pool)
        
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.check_interval = check_interval
        self.on_disconnect = on_disconnect
        self.max_mqtt_retries = max_mqtt_retries
        self.running = True
        self.mqtt_connected = False
    
    async def send_command(self, command, expected_prefix=None, timeout=5):
        """Send an AT command and collect its full response."""
        print(f"send at command {command}")
        async with self.lock:  # Ensure only one command runs at a time
            self.response_buffer = []  # Clear previous responses
            self.response_event.clear()

            self.uart.write((command + "\r\n").encode())  # Send AT command

            try:
                await asyncio.wait_for(self.response_event.wait(), timeout=timeout)
                response = "\n".join(self.response_buffer).strip()
                if expected_prefix:
                    response = "\n".join(line for line in self.response_buffer if line.startswith(expected_prefix))
                print("response ", response)
                return response
            except asyncio.TimeoutError:
                return None  # No response received in time


    async def read_responses(self):
        """Continuously read responses from the GSM module."""
        while True:
            #print("Read responses")
            if self.uart.in_waiting:  # Check if data is available
                line = self.uart.readline()
                if line:
                    decoded_line = line.decode().strip()
                    self.response_buffer.append(decoded_line)
                    print("read_responses: Appending ", decoded_line)
                    # End response when we get "OK" or "ERROR"
                    if decoded_line in ("OK", "ERROR"):
                        self.response_event.set()

            await asyncio.sleep(0)  # Yield to other tasks



    async def setup_gsm(self):

        asyncio.create_task(self.read_responses())  # 🔹 Runs in the background
        await self.send_command("AT")
        await self.send_command("ATE0")
        await self.send_command("AT+CMGF=1")
        await self.send_command("AT+CNMI=2,0,0,0,0") # Store SMS (instead of AT+CNMI=2,2,0,0,0)
        await self.send_command("AT+CMGD=1,4")
        

    
    async def send_sms(self, number, message):
        await self.send_command(f'AT+CMGS="{number}"')
        self.uart.write((message + "\x1A").encode())
        await asyncio.sleep(3)
    
    async def read_sms_deprecated(self):
        print("read_sms: start...")
        response = await self.send_command("AT+CMGL=\"REC UNREAD\"")
        messages = []
        #print(f"<{response}>")
        
        if response.startswith('+CMGL: '):
            parts = response[:-2].split(',')
            print(len(parts))
            msg_number = parts[0].split(':')[1].strip()
            sender = parts[2].strip('"')
            dt = parts[4]
            content = parts[5:]
            
            #    content = response[i + 1]
                #messages.append((msg_number, sender, content))

        print("read_sms: Messges = ", messages)
        
        return messages
    
    
    async def read_sms(self):
    
        sms_response = await self.send_command("AT+CMGL=\"REC UNREAD\"")
        #print("READ SMS-------->", sms_response)
        
        # Split and exclude empty first and "OK" at the end
        raw_messages = sms_response[:-2].split("+CMGL:")[1:]
        #print("RAW MESSAGES ", raw_messages)
        
        messages = []
        for msg in raw_messages:
            parts = msg.strip().split("\n", 1)  # Split metadata and content
            metadata = parts[0].split(",")
            content = parts[1].strip() if len(parts) > 1 else ""  # Message body

            index = metadata[0].strip()
            sender = metadata[2].strip('"')
            timestamp = metadata[4].strip('"')

            messages.append({"index": index, "sender": sender, "timestamp": timestamp, "message": content})

        # Print parsed messages
        for msg in messages:
            print(f"Index: {msg['index']}, Sender: {msg['sender']}, Time: {msg['timestamp']}")
            print(f"Message: {msg['message']}\n")
            set_message_area(msg['message'])
                             
        return messages
        
    async def delete_sms(self, index):
        await self.send_command(f'AT+CMGD={index}')
    
    def on_mqtt_connect(self, client, userdata, flags, rc):
        print("Connected to MQTT broker")
        self.mqtt_connected = True
        self.mqtt_client.subscribe(os.getenv('SMS_SEND_TOPIC'))
    
    def on_mqtt_disconnect(self, client, userdata, rc):
        print("MQTT broker disconnected.")
        self.mqtt_connected = False
    
    def on_mqtt_message(self, client, userdata, msg):
        try:
            js = json.loads(msg)
            #print("on_mqtt_message", js)
            #number, message = data.split(',', 1)
            asyncio.create_task(self.send_sms(js['to'], js['text']))
        except Exception as e:
            print("Error processing MQTT message:", e)

    def build_sms_mqtt_message(self, sender, content, tstamp):

        js = json.dumps({
             "to": "you",
             "from": sender,
             "time": tstamp,
             "text": content
        })
        
        return js # f"{tstamp},{sender},{content}"
    
    async def forward_sms_to_mqtt(self):

        topic = os.getenv("SMS_RECEIVED_TOPIC") #
        
        while self.running:
            if self.mqtt_connected:
                messages = await self.read_sms()
                for msg in messages:
                    msg_number = msg['index']
                    sender = msg['sender']
                    content = msg['message']
                    tstamp = msg['timestamp']
                    
                    self.mqtt_client.publish(topic, self.build_sms_mqtt_message(sender, content, tstamp))
                    await self.delete_sms(msg_number)
            await asyncio.sleep(POLL_SMS)

    def is_connected(self, response):
        print("Checking network - ", response)
        connected =  list(filter(lambda  _: ',1' or ',5' in _, list(filter(lambda _ :  '+CREG: 0,' in  _, response))))
        print(connected, len(connected) > 0)
        return len(connected) > 0

    async def check_network(self):
        """Periodically check if the module is connected to a network."""
        while self.running:
            response = await self.send_command("AT+CREG?", expected_prefix="+CREG:")
            if response:
                print(f"Network Status:\n{response}")
                #if not self.is_connected(response):
                #    print("GSM Disconnected!")
                #    if self.on_disconnect:
                #        self.on_disconnect("GSM Network Failure")
                
            await asyncio.sleep(self.check_interval)


    async def check_network_deprecated(self):
        while self.running:
            print("check_network")
            #response = await self.send_command("AT+CREG?")
            response = await self.send_command("AT+CREG?", expected_prefix="+CREG:")
            
            #if "+CREG: 0,1" not in response and "+CREG: 0,5" not in response:
            #if not any(any(status in _ for status in (',1', ',5')) for _ in response if '+CREG:' in _):
            if not self.is_connected(response):
                print("GSM Disconnected!")
                if self.on_disconnect:
                    self.on_disconnect("GSM Network Failure")
            
            await asyncio.sleep(self.check_interval)
    
    async def maintain_mqtt_connection(self):
        retries = 0
        while self.running:
            print("maintain_mqtt_connection")
            self.mqtt_client.loop()  # Process MQTT messages
            
            if not self.mqtt_connected:
                if retries >= self.max_mqtt_retries:
                    print("MQTT connection failed after multiple attempts. Aborting...")
                    if self.on_disconnect:
                        self.on_disconnect("MQTT Network Failure")
                    return
                try:
                    print("Reconnecting to MQTT broker... (Attempt {} of {})".format(retries + 1, self.max_mqtt_retries))
                    self.mqtt_client.reconnect()
                    retries = 0  # Reset retries on success
                except Exception as e:
                    print("Failed to reconnect MQTT broker:", e)
                    retries += 1
            await asyncio.sleep(10)
    
    async def start(self, update_status_window):
        print("GSM START**********")
        #self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, 60)
        self.mqtt_client.connect()
        #self.mqtt_client.loop_start()
        await asyncio.gather(
            self.forward_sms_to_mqtt(),
            self.check_network(),
            self.maintain_mqtt_connection(),
            update_status_window()
        )
    
    def stop(self):
        self.running = False
        #self.mqtt_client.loop_stop()
        self.uart.deinit()

if __name__ == "__main__":
    
    
    async def update_status_window():
        
        def size_of_text(message):
             return len(message)
        UPDATE_DIVISOR = 4
        PIXELS_PER_UPDATE = 4
        count = 0 
        while True:
            count = count + 1
            if ('count_area' in globals()):
                count_area.text = str(count//UPDATE_DIVISOR)
            if ('message_area' in globals()):
                message_area.x = TEXT_OFFSET - PIXELS_PER_UPDATE * (count % size_of_text(message_area.text))
            
            await asyncio.sleep((1.0/UPDATE_DIVISOR))
            
    def build_pin(pin_number):
        pin = getattr(board, f"IO{pin_number}")  # Convert string to actual board pin
        return pin

    def machine_pin(pin_no,direction):
        pin = digitalio.DigitalInOut(pin_no)  # Change GP15 to your LED pin
        pin.direction = direction
        return pin

    def machine_pin_deprecated(pin_no,direction):
        pin = digitalio.DigitalInOut(build_pin(pin_no))  # Change GP15 to your LED pin
        pin.direction = direction
        return pin

    def sleep_ms(ms):
        time.sleep(ms / 1000)  # Convert milliseconds to seconds


    def handle_disconnect(reason):
            print(f"Attempting to reconnect due to: {reason}")
    
    tries  = 0
    #time.sleep(5)
    #microcontroller.reset()
    
    while True:
        try:
            wifi.radio.connect(os.getenv('CIRCUITPY_WIFI_SSID'), os.getenv('CIRCUITPY_WIFI_PASSWORD'))
            print("Connected to WiFi")
            break 
        except Exception as e:
            tries = tries + 1
            if tries > 5:
                microcontroller.reset(0)
            time.sleep(5)
    print("Create Socket Pool")
    
    pool = socketpool.SocketPool(wifi.radio)


    requests = adafruit_requests.Session(pool, ssl.create_default_context())

    #  prints MAC address to REPL
    print("My MAC addr:", [hex(i) for i in wifi.radio.mac_address])

    #  prints IP address to REPL
    print("My IP address is", wifi.radio.ipv4_address)

    #  pings Google
    ipv4 = ipaddress.ip_address("8.8.4.4")
    print("Ping google.com: %f ms" % (wifi.radio.ping(ipv4)*1000))
    try:
            print("Fetching text from %s" % quotes_url)
            #  gets the quote from adafruit quotes
            response = requests.get(quotes_url)
            print("-" * 40)
            #  prints the response to the REPL
            print("Text Response: ", response.text)
            print("-" * 40)
            response.close()
    except Exception as e:
            print("Error:\n", str(e))
            print("Resetting microcontroller in 10 seconds")
            time.sleep(10)
            #microcontroller.reset()
   
    GSM_POWER = machine_pin(MODEM_POWER_PIN, digitalio.Direction.OUTPUT)
    GSM_POWER.value = True


#LED = machine.Pin(LED_PIN, machine.Pin.OUT)
#LED.value(1)

#if MODEM_RST > 0:
    modem_reset = machine_pin(MODEM_RST, digitalio.Direction.OUTPUT)
    modem_reset.value = True


    gsm_pwr = machine_pin(MODEM_PWRKEY_PIN, digitalio.Direction.OUTPUT)
    gsm_pwr.value=True
    sleep_ms(200)
    gsm_pwr.value=False
    sleep_ms(1000)
    gsm_pwr.value=True
    
    uart = busio.UART(MODEM_TX, MODEM_RX, baudrate=9600, timeout=2)
    gsm = GSMModule(uart, pool, mqtt_broker = os.getenv('MQTT_HOST'), mqtt_user = os.getenv('MQTT_USER'), mqtt_password = os.getenv('MQTT_PASSWORD'))
    
    asyncio.run(gsm.setup_gsm())
    
    try:
        asyncio.run(gsm.start(update_status_window))
    except KeyboardInterrupt:
        print("Stopping GSM module...")
        gsm.stop()

