"""liquidctl drivers for Corsair Commander Pro device.

Supported devices:

- Corsair Commander Pro

Copyright (C) 2020–2020  Jonas Malaco and contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

# For details on protocol, see this thread on OpenCorsairLink.
# https://github.com/audiohacked/OpenCorsairLink/issues/70

# Example usage:
# set LED mode to rainbow on channel 1
# python liquidctl.cli --match commander set CHANNEL_1 color RAINBOW ff0000

# set speed pwm to 75%
# python liquidctl.cli --match commander set 0 speed 75

import itertools
import logging
import re

from enum import Enum, unique

from liquidctl.driver.usb import UsbHidDriver
from liquidctl.keyval import RuntimeStorage
from liquidctl.pmbus import compute_pec
from liquidctl.util import clamp, fraction_of_byte, u16le_from, normalize_profile

# Constants
_WRITE_LENGTH = 64
_READ_LENGTH = 16
LOGGER = logging.getLogger(__name__)

@unique
class _Command(Enum):
    # --------------------------------------------------------------------
    # Diagnostics
    # --------------------------------------------------------------------

    # GET device status
    # Not implemented, more research required.
    READ_STATUS = 0x01

    # GET firmware version
    GET_FIRMWARE_VERSION = 0x02

    # GET software ID
    GET_SOFTWARE_ID = 0x03

    # SET device ID
    WRITE_DEVICE_ID = 0x04

    # Start firmware update
    START_FIRMWARE_UPDATE = 0x05

    # GET bootloader version
    GET_BOOTLOADER_VERSION = 0x06

    # Write test flag
    WRITE_TEST_FLAG = 0x07

    # --------------------------------------------------------------------
    # Temperatures
    # --------------------------------------------------------------------

    # GET temperature sensor config. (Connected/Disconnected)
    GET_THERMOMETER_CONFIG = 0x10

    # GET temperature (for each of the connected sensors)
    GET_TEMPERATURE = 0x11

    # GET voltage (Measure 12V, 5V or 3.3V rail)
    GET_VOLTAGE = 0x12

    # --------------------------------------------------------------------
    # Fans
    # --------------------------------------------------------------------

    # GET fan mode configuration (Auto/Disconnected, 3-pin or 4-pin)
    GET_FAN_CONFIG = 0x20

    # GET fan speed in RPM
    GET_FAN_SPEED = 0x21

    # GET fan speed in %
    GET_FAN_SPEED_PWM = 0x22

    # Set fan speed (pwm) in %
    SET_FAN_SPEED_PERCENT = 0x23

    # SET fan speed (Fixed RPM)
    SET_FAN_SPEED_RPM = 0x24

    # SET fan configuration (Graph)
    SET_FAN_SPEED_GRAPH = 0x25

    # SET fan temperature info (if the group is chosen to be an external
    # sensor)
    SET_FAN_TEMP_INFO = 0x26

    # SET fan force (3 pin mode)
    # Not implemented, not sure what it does. More research is required.
    SET_FAN_FORCE = 0x27

    # SetFanDetectionType
    SET_FAN_MODE = 0x28

    # ReadFanDetectionType
    # Not implemented, not sure what it does. See here for explanation:
    # https://github.com/audiohacked/OpenCorsairLink/issues/70#issuecomment-504702241
    GET_FAN_MODE = 0x29

    # --------------------------------------------------------------------
    # LEDs
    # --------------------------------------------------------------------

    # ReadLedStripMask
    GET_LED_STRIP_MASK = 0x30

    # WriteLedRgbValue
    # Not sure what this does - possibly used to apply LED changes.
    # More research required.
    SET_LED_VALUE = 0x31

    # WriteLedColorValues
    # Not sure what this does, perhaps used by other non-commander pro
    # devices. More research is required.
    SET_LED_COLOUR_VALUES = 0x32

    # Apply LED changes.
    SET_LED_TRIGGER = 0x33

    # WriteLedClear
    SET_LED_CLEAR = 0x34

    # WriteLedGroupSet
    SET_LED_MODE = 0x35

    # WriteLedExternalTemp
    SET_LED_TEMP_INFO = 0x36

    # WriteLedGroupsClear
    SET_LED_GROUP_CLEAR = 0x37

    # WriteLedMode
    SET_LED_GROUP_MODE = 0x38

    # WriteLedBrightness
    SET_LED_BRIGHTNESS = 0x39

    # WriteLedCount
    # Not sure what this does, more research is required.
    GET_LED_COUNT = 0x3a

    # WriteLedPortType
    # Not implemented. 0x3B commands seems to only get transmitted
    # after a change of LED Channel mode, such as a change from "RGB
    # LED Strip" to "RGB HD Fan".
    SET_LED_PORT_TYPE = 0x3B

# --------------------------------------------------------------------
# LED Channels
# --------------------------------------------------------------------
class _Channel(Enum):
    # LED Channel 1
    CHANNEL_1 = 0x00

    # LED Channel 2
    CHANNEL_2 = 0x01

# --------------------------------------------------------------------
# LED FAN IDs
# --------------------------------------------------------------------
class _FanID(Enum):
    # LED Fan 1
    FAN_1 = 0x00

    # LED Fan 2
    FAN_2 = 0x0C

    # LED Fan 3
    FAN_3 = 0x18

    # LED Fan 4
    FAN_4 = 0x24

    # LED Fan 5
    FAN_5 = 0x30

    # LED Fan 6
    FAN_6 = 0x3C

# --------------------------------------------------------------------
# LED Types
# --------------------------------------------------------------------
class _LedType(Enum):
    # RGB LED Strip
    STRIP = 0x0A

    # RGB HD Fan
    HD_FAN = 0x0C

    # RGB SP Fan
    SP_FAN = 0x01

    # RGB ML Fan
    M1_FAN = 0x04

# --------------------------------------------------------------------
# LED Effects
# --------------------------------------------------------------------
class _Effect(Enum):
    # Rainbow wave LED effect
    RAINBOW_WAVE = 0x00

    # Colour shift LED effect
    COLOUR_SHIFT = 0x01

    # Colour pulse LED effect
    COLOUR_PULSE = 0x02

    # Colour wave LED effect
    COLOUR_WAVE = 0x03

    # Static LED colour
    STATIC = 0x04

    # Temperature LED effect
    TEMPERATURE = 0x05

    # Visor LED effect
    VISOR = 0x06

    # Marquee LED effect
    MARQUEE = 0x07

    # Blink LED effect
    BLINK = 0x08

    # Sequential (channel effect)
    SEQUENTIAL = 0x09

    # Rainbow LED effect
    RAINBOW = 0x0A

# --------------------------------------------------------------------
# LED Effect Speeds
# --------------------------------------------------------------------
class _Speed(Enum):
    # Fast LED speed
    HIGH = 0x00

    # Medium LED speed
    MEDIUM = 0x01

    # Slow LED speed
    SLOW = 0x02

# --------------------------------------------------------------------
# LED Effect Directions
# --------------------------------------------------------------------
class _Direction(Enum):
    # Backwards direction for LED effects.
    BACKWARD = 0x00

    # Forwards direction for LED effects.
    FORWARD = 0x01

# --------------------------------------------------------------------
# LED Colour Modes
# --------------------------------------------------------------------
class _ColourMode(Enum):
    # Alternating colours
    ALTERNATING = 0x00

    # Random colours
    RANDOM = 0x01

# --------------------------------------------------------------------
# LED Brightnesses
# --------------------------------------------------------------------
class _Brightness(Enum):
    # 100% Brightness
    MAX = 0x64

    # 66% Brightness
    MEDIUM = 0x42

    # 33% Brightness
    LOW = 0x21

    # 0% Brightness
    ZERO = 0x00

class LedConfig:
    """LED configuration"""
    def __init__(self, channel, ledType, brightness, fanConfigs):
        self.channel = channel
        self.ledType = ledType
        self.brightness = brightness
        self.fanConfigs = fanConfigs
    
    def apply(self, device):
        """Apply this configuration to a device"""
        init1 = bytearray(_WRITE_LENGTH)
        init1[0] = _Command.SET_LED_GROUP_CLEAR.value

        init2 = bytearray(_WRITE_LENGTH)
        init2[0] = _Command.SET_LED_CLEAR.value

        init3 = bytearray(_WRITE_LENGTH)
        init3[0] = _Command.SET_LED_BRIGHTNESS.value
        init3[1] = 0x00
        init3[2] = self.brightness.value
        
        init4 = bytearray(_WRITE_LENGTH)
        init4[0] = _Command.SET_LED_GROUP_MODE.value
        init4[1] = 0x00
        init4[2] = 0x01

        final = bytearray(_WRITE_LENGTH)
        final[0] = _Command.SET_LED_TRIGGER.value
        final[2] = 0xff

        device.clear_enqueued_reports()

        device.write(init1)
        device.read(_READ_LENGTH)

        device.write(init2)
        device.read(_READ_LENGTH)

        device.write(init3)
        device.read(_READ_LENGTH)

        device.write(init4)
        device.read(_READ_LENGTH)

        # buf contains the config for each fan
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_LED_MODE.value
        buf[1] = self.channel.value
        buf[3] = self.ledType.value
        buf[8] = 0xff

        for fanConfig in self.fanConfigs:
            buf[2] = fanConfig.fan.value
            buf[4] = fanConfig.effect.value
            buf[5] = fanConfig.speed.value
            buf[6] = fanConfig.direction.value
            buf[7] = fanConfig.colourMode.value

            # If colour mode is random, bytes 9-17 are all 0.
            if fanConfig.colourMode != _ColourMode.RANDOM:
                buf[9] =  fanConfig.colour1[0] # colour1.red
                buf[10] = fanConfig.colour1[1] # colour1.green
                buf[11] = fanConfig.colour1[2] # colour1.blue
                buf[12] = fanConfig.colour2[0] # colour2.red
                buf[13] = fanConfig.colour2[1] # colour2.green
                buf[14] = fanConfig.colour2[2] # colour2.blue
                buf[15] = fanConfig.colour3[0] # colour3.red
                buf[16] = fanConfig.colour3[1] # colour3.green
                buf[17] = fanConfig.colour3[2] # colour3.blue

            device.write(buf)
            device.read(_READ_LENGTH)
        
        device.write(final)
        device.read(_READ_LENGTH)

class FanConfig:
    """Fan configuration"""
    def __init__(self, fan, effect, speed, direction, colourMode, colour1, colour2, colour3):
        self.fan = fan
        self.effect = effect
        self.speed = speed
        self.direction = direction
        self.colourMode = colourMode
        self.colour1 = colour1
        self.colour2 = colour2
        self.colour3 = colour3

class CommanderPro(UsbHidDriver):
    """Corsair Commander Pro driver"""
    SUPPORTED_DEVICES = [
        (0x1B1C, 0x0C10, None, 'Corsair Commander Pro (experimental)',
        {'fan_count': 6, 'rgb_fans': True}),
    ]

    def __init__(self, device, description, fan_count, rgb_fans, **kwargs):
        super().__init__(device, description, **kwargs)
        self._fan_names = [f'fan{i + 1}' for i in range(fan_count)]

    def connect(self, **kwargs):
        """Connect to the device."""
        super().connect(**kwargs)
        #self.num_fans = self.get_num_connected_fans()
        LOGGER.info('Commander Pro info:')
        LOGGER.info('Firmware version = %s' % (self.get_firmware_version()))
        LOGGER.info('Bootloader version = %s' % (self.get_bootloader_version()))
        LOGGER.info('Software version = %s' % (self.get_software_id()))
    
    def initialize(self, **kwargs):
        """Initialize the device."""
        super().initialize(**kwargs)
    
    def disconnect(self, **kwargs):
        """Disconnect from the device."""
        super().disconnect(**kwargs)
    
    def get_status(self, **kwargs):
        """Get a status report.
        
        Returns a list of `(property, value, unit)` tuples.
        """
        self.get_led_status(_Channel.CHANNEL_1.value)
        self.get_led_status(_Channel.CHANNEL_2.value)

        properties = self.get_fan_status()
        properties.extend(self.get_thermometer_status())
        properties.extend(self.get_power_status())
        return properties

    # --------------------------------------------------------------------
    # LED Control
    # --------------------------------------------------------------------
    def set_color(self, channel, mode, colors, **kwargs):
        """Set the colour mode for a specific channel.
        
        channel should be CHANNEL_1 or CHANNEL_2
        mode should be one of the _Effect values (e.g. RAINBOW_WAVE)
        colors should be 1 - 3 colours.

        Currently, this applies the changes to ALL fans, because the
        current implementation will set all other fans' LEDs to blank
        whenever we apply changes to any fans.

        This is a limitation of the integration between this function and
        the CLI. Directly using the FanConfig API from python allows for
        much more complicated LED configurations.

        Furthermore, several features, such as direction (forwards vs
        backwards), colour mode (random vs alternating), etc are
        unavailable from the CLI.
        """
        # Values from UI
        effect = _Effect[mode]
        channel = _Channel[channel]
        colours = list(colors)

        if len(colours) < 1:
            raise ValueError('at least 1 colour must be provided')
        if len(colours) > 3:
            raise ValueError('at most 3 colours can be provided')

        while len(colours) < 3:
            colours.append([0, 0, 0])

        # fixme: these properties are not accessible from CLI at present.
        # need to figure out how 
        speed = _Speed.HIGH
        fans = [_FanID.FAN_1, _FanID.FAN_2, _FanID.FAN_3, _FanID.FAN_4, _FanID.FAN_5, _FanID.FAN_6]
        direction = _Direction.FORWARD
        colourMode = _ColourMode.ALTERNATING
        
        # Generate a configuration for each fan based on UI.
        configs = []
        for fan in fans:
            config = FanConfig(fan, effect, speed, direction, colourMode, colours[0], colours[1], colours[2])
            configs.append(config)

        configuration = LedConfig(channel, _LedType.HD_FAN, _Brightness.MAX, configs)

        # Apply the chosen LED configuration
        configuration.apply(self.device)

    def notify_led_temperature(self, channel, temperature):
        """Notify the LEDs of the temperature

        When the LED mode is set to "temperature", corsair LINK will spam
        the device with these requests, in order to notify it of the
        current temperature, which it then uses to adjust the LED colour.

        channel: 0 for channel 0, 1 for channel 1.
        temperature: current temperature in °C ([0..65535]).
        """
        if channel < 0 or channel > 1:
            raise ValueError('channel must be 0 or 1')
        if temperature < 0 or temperature > 65535:
            raise ValueError('temperature must be in range [0..65535]')

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_LED_TEMP_INFO.value
        buf[1] = channel
        # byte 2 is always 0
        temperature_bytes = int.to_bytes(temperature, length = 2, byteorder = 'big')
        buf[3] = temperature_bytes[0]
        buf[4] = temperature_bytes[1]

        self.device.write(buf)
        # response should be all 0.
        resp = self.device.read(_READ_LENGTH)

    def get_port_led_count(self):
        """Not sure what this does, more research is required
        """

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_LED_COUNT.value

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        # response is all 0. No idea what it means.
        resp = self.device.read(_READ_LENGTH)

    # --------------------------------------------------------------------
    # Fan control
    # --------------------------------------------------------------------
    def set_speed_profile(self, channel, profile, **kwargs):
        """Set channel to follow a speed duty profile.

        Essentially, the fan speed is based on a linear interp of temperature.

        channel should be a fan number (0..5 for fan 1..6)
        profile should be 6 pairs of (temperature, percent) (in °C).
        """
        fan_number = int(channel)

        # The commander pro implements this functionality on an RPM basis.
        # Therefore, we convert fan speeds from pwm to rpm.
        temperatures = []
        speeds = []
        for temperature, pwm in profile:
            temperatures.append(temperature)
            speeds.append(pwm * 25)

        # set_speed_graph() expects exactly 6 speeds and temps. We need
        # to pad out this arrays until we have 6 of each.
        if len(temperatures) > 0 and len(speeds) > 0:
            final_temp = temperatures[len(temperatures) - 1]
            final_speed = speeds[len(speeds) - 1]
            while len(temperatures) < 6:
                temperatures.append(final_temp)
            while len(speeds) < 6:
                speeds.append(final_speed)

        self.set_speed_graph(0, fan_number, temperatures, speeds)

    def set_speed_percent(self, channel, duty):
        """Set channel to a fixed speed duty.
        
        `channel` should be an int in range [0, 5]
        `duty` should be the speed in rpm (<2500)
        """
        if channel < 0 or channel > 5:
            raise ValueError("Channel must be in range [0, 5]")

        # I have no idea what happens if we try and set speed to, say, 1000%.
        if duty > 100 or duty < 0:
            raise ValueError("Fan speed greater than 100%% or less than 0%% is impossible")

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_FAN_SPEED_PERCENT.value
        buf[1] = channel
        buf[2] = duty

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

    def set_fixed_speed(self, channel, duty, **kwargs):
        """Set channel to a fixed speed duty.
        
        `channel` should be an int in range [0, 5]
        `duty` should be the speed in pwm (%).
        """
        channel_index = int(channel)
        speed = int(duty)
        self.set_speed_percent(channel_index, speed)

    def set_speed_rpm(self, fan, speed):
        """Set speed for a fan to a fixed RPM value.

        fan: fan number (0..5 for fan 1..6)
        speed: speed in RPM
        """
        if fan < 0 or fan > 5:
            raise ValueError('fan must be in range [0, 5]')
        
        if speed < 0:
            raise ValueError('Negative speed not allowed')
        if speed > 2500:
            # From my testing, setting the RPM speed to a very high or low
            # value (e.g. 20) will put the fan into PWM mode at 100% power.
            LOGGER.warning('speed greater than 2500 may be bad idea')
        if speed > 65535:
            raise ValueError('speed must be < 65535')

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_FAN_SPEED_RPM.value
        buf[1] = channel_index
        speed_bytes = int.to_bytes(speed, length = 2, byteorder = 'big')
        buf[2] = speed_bytes[0]
        buf[3] = speed_bytes[1]

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

    def set_speed_graph_example(self):
        temps = [20, 30, 40, 60, 80, 100]
        speeds = [800, 1000, 1250, 1500, 2000, 2500]

        fan = 0
        thermometer = 0
        for fan in range(0, 6):
            self.set_speed_graph(0, fan, temps, speeds)

    def set_speed_graph(self, thermometer, fan, temps, speeds):
        """This sets the fan mode to 'graph' configuration.

        Essentially, the fan speed is based on a linear interp of temperature.

        `fan` should be the fan number (0-5 for fan 1-6).
        `thermometer` should be thermometer index (0-3 for thermometer 1-4).
        `temps` should be an array of 6 ints representing temperatures in °C.
        `speeds` should be an array of 6 ints representing fan speeds in RPM.
        """
        if fan < 0 or fan > 6:
            raise ValueError('fan number should be in range [0, 5]')

        if thermometer < 0 or thermometer > 3:
            raise ValueError('thermometer should be in range [0, 3]')

        if len(temps) != 6:
            raise ValueError('Need to provide 6 temperatures, only %d were given' % len(temps))

        if len(speeds) != 6:
            raise ValueError('Need to provide 6 speeds, only %d were given' % len(speeds))
        
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_FAN_SPEED_GRAPH.value
        buf[1] = fan
        buf[2] = thermometer

        for i in range(0, 6):
            temp_bytes = int.to_bytes(temps[i] * 100, length = 2, byteorder = 'big')
            speed_bytes = int.to_bytes(speeds[i], length = 2, byteorder = 'big')

            buf[3 + 2 * i] = temp_bytes[0]
            buf[4 + 2 * i] = temp_bytes[1]
            buf[15 + 2 * i] = speed_bytes[0]
            buf[16 + 2 * i] = speed_bytes[1]
            #buf[3 + 2 * i:4 + 2 * i] = temp_bytes
            #buf[15 + 2 * i:16 + 2 * i] = speed_bytes

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

    def send_temperature_info(self, sensor_index, temperature):
        """Send temperature info to the device.

        When fan is in "Graph" mode (see se_speed_graph), the temperature
        can be manually provided by this function, usually in order to
        integrate with an external temperature sensor. In this mode,
        corsair link will spam the device with 0x26 commands, which tells
        the device what the temperature is, so it can adjust its own fan
        speed.

        The usefulness of this seems questionable, but an implementation of
        this feature is provided here nonetheless, for completeness' sake.

        sensor_index is the sensor number (0-indexed)
        temperature is the temperature in °C.
        """
        if sensor_index > 255 or sensor_index < 0:
            raise ValueError('sensor_index must be in range [0, 255] (probably less)')

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_FAN_TEMP_INFO.value
        buf[1] = sensor_index
        temp_bytes = int.to_bytes(temperature * 100, length = 2, byteorder = 'big')
        buf[2] = temp_bytes[0]
        buf[3] = temp_bytes[1]

        self.device.write(buf)
        # Response should be all zeroes
        resp = self.device.read(_READ_LENGTH)

    def set_fan_mode(self, fan, mode):
        """Sets the mode/configuration of the fan.

        fan: fan number (0..5 for fan 1..6).
        mode: 0 = auto, 1 = 3-pin, 2 = 4-pin

        No idea what this does, nor is it tested.
        """
        if fan < 0 or fan > 5:
            raise ValueError('fan must be in range [0..5]')

        if mode < 0 or mode > 2:
            raise ValueError('mode must be in range [0..2]')

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.SET_FAN_MODE.value
        buf[1] = 0x02 # constant
        buf[2] = fan
        buf[3] = mode

        self.device.write(buf)
        # Response should be all zeroes.
        resp = self.device.read(_READ_LENGTH)

    # --------------------------------------------------------------------
    # Low level version info stuff
    # --------------------------------------------------------------------
    def get_firmware_version(self):
        """Get the firmware version.

        The firmware version is a string of the form X.Y.Z
        """
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_FIRMWARE_VERSION.value
        
        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        # The response is interpreted as follows:
        # Byte 0 is always zero.
        # Byte 1 is X.
        # Byte 2 is Y.
        # Byte 3 is Z.
        # The firmware version can then be interpreted as X.Y.Z. Example 0x0004AD => 0.4.173
        return '%d.%d.%d' % (resp[1], resp[2], resp[3])

    def get_software_id(self):
        """Get the software ID.
        
        This is a string of the form W.X.Y.Z
        """
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_SOFTWARE_ID.value

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        # I have no idea what this actually means, this implementation
        # is copied straight from OpenCorsairLink. I don't think those
        # guys know what it is either :).
        return '%d.%d.%d.%d' % (resp[1], resp[2], resp[3], resp[4])
    
    def get_bootloader_version(self):
        """Get the bootloader version.

        This is a string of the form X.Y
        """
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_BOOTLOADER_VERSION.value

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        return '%d.%d' % (resp[1], resp[2])
    
    # --------------------------------------------------------------------
    # Get status functionality
    # --------------------------------------------------------------------
    def get_fan_status(self):
        """Get the status of the connected fans.

        Returns a list of `(property, value, unit)` tuples.
        """
        # Get number of fans connected.
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_FAN_CONFIG.value

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        """
        The responses is an indication of the fan configuration in the following format:
        Byte 0 is always zero.
        Byte 1-6 describes mode for each fan (6 fan connectors are available in the Commander Pro):
        0x00 => Auto/Disconnected
        0x01 => 3-pin
        0x02 => 4-pin
        0x03 => ?
        """
        properties = []
        for i in range(1, 7):
            # Get fan status
            propertyName = 'Fan %d' % i
            if resp[i] == 0x00:
                status = 'disconnected'
            elif resp[i] == 0x01:
                status = 'connected (3 pins)'
            elif resp[i] == 0x02:
                status = 'connected (4 pins)'
            else: # typically this is 0x03, no idea what it means though
                status = 'unknown' # more research required 
            properties.append((propertyName + ' status', status, ''))

            if resp[i] == 0x01 or resp[i] == 0x02:
                speed = self.get_fan_speed_rpm(i - 1)
                properties.append((propertyName + ' speed', speed, 'rpm'))


        return properties

    def get_fan_speed_rpm(self, fanIndex):
        """Get the speed (in RPM) of a fan at a given index (in range [0, 5])"""

        # The second byte (byte 1) is the fan number, (0-5 for fan 1-6).
        # All other bytes are zero.
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_FAN_SPEED.value
        buf[1] = fanIndex
        
        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        # The response begins with 0x00 followed by 2-byte RPM in big endian.
        # All other bytes are zero.
        return int.from_bytes(resp[1:3], byteorder = 'big')

    def get_fan_speed_pwm(self, fanIndex):
        """Get the speed (in %) of a fan at a given index (in range [0, 5])"""

        # The second byte (byte 1) is the fan number, (0-5 for fan 1-6).
        # All other bytes are zero.
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_FAN_SPEED_PWM.value
        buf[1] = fanIndex
        
        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        # The second byte of the response (byte 1) is the speed in %.
        return resp[1]

    def get_thermometer_status(self):
        """
        Gets the status of the thermometers.

        Returns a list of `(property, value, unit)` tuples.
        """
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_THERMOMETER_CONFIG.value    

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        config = []
        # Byte 0 is always zero. Byte 1-4 indicates if sensor 1-4 is connected
        # or not: 0x00 => not connected, 0x01 => connected.
        for i in range(1, 5):
            propertyName = 'Thermometer %d' % i
            if resp[i] == 0x00:
                status = 'disconnected'
            elif resp[i] == 0x01:
                status = 'connected'
            else:
                status = 'unknown' # Should never happen

            config.append((propertyName + ' status', status, ''))

            if resp[i] == 0x01:
                temp = self.get_temperature(i - 1)
                config.append((propertyName + ' temperature', temp, '°C'))
        return config

    def get_temperature(self, index):
        """Get the temperature of the specified thermometer in °C.
        
        index: index of the thermometer (0..3) for thermometer (1..4)
        """
        if index < 0 or index > 3:
            raise ValueError('Thermometer index must be in range [0, 3]')

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_TEMPERATURE.value
        buf[1] = index

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        # Byte 1 and 2 is the temperature as a two-byte big endian number.
        # The temperature can be converted to degrees Celsius by dividing by 100.
        return int.from_bytes(resp[1:3], byteorder = 'big') / 100

    def get_led_status(self, channel):
        """This is some sort of status report for LEDs.

        I have no idea what the response means. For now this function
        returns nothing.
        """
        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_LED_STRIP_MASK.value
        buf[1] = channel

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        # Who knows wtf this means...
        resp = self.device.read(_READ_LENGTH)
    
    def get_power_status(self):
        """
        Get the current voltage on the various rails.

        Returns a list of `(property, value, unit)` tuples.
        """
        properties = []
        properties.append(('12V  rail', self.get_voltage(0), 'V'))
        properties.append(('5V   rail', self.get_voltage(1), 'V'))
        properties.append(('3.3V rail', self.get_voltage(2), 'V'))
        return properties

    def get_voltage(self, sensor_index):
        """Get the voltage for the specified sensor/rail in volts.

        sensor_index should be rail number:

        Rail 0: 12 V
        Rail 1: 5 V
        Rail 2: 3.3 V
        """
        if sensor_index < 0 or sensor_index > 2:
            raise ValueError('sensor_index must be in range [0, 2]')

        buf = bytearray(_WRITE_LENGTH)
        buf[0] = _Command.GET_VOLTAGE.value    
        buf[1] = sensor_index

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        resp = self.device.read(_READ_LENGTH)

        # Bytes 1 and 2 are voltage in big endian. The actual voltage can be
        # calculated by dividing the two-byte number by 1000.
        return int.from_bytes(resp[1:3], byteorder = 'big') / 1000
