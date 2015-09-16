#!/usr/bin/env python

import os
import sys
import serial
import re
from datetime import datetime as dt
from datetime import timedelta as tdelta
import time
import argparse
import logging
import logging.handlers
import numpy as np

import astropy.units as u
import astropy.table as table
import astropy.io.ascii as ascii
from astropy.time import Time

import panoptes
from panoptes.utils.config import load_config
from panoptes.utils.database import PanMongo
# from panoptes.utils.PID import PID
from panoptes.weather.weather_station import WeatherStation

##-----------------------------------------------------------------------------
## Quick moving average function
##-----------------------------------------------------------------------------
def movingaverage(interval, window_size):
    window= np.ones(int(window_size))/float(window_size)
    return np.convolve(interval, window, 'same')


##-----------------------------------------------------------------------------
## PID Class
##-----------------------------------------------------------------------------
class PID:
    '''
    Pseudocode from Wikipedia:

    previous_error = 0
    integral = 0
    start:
      error = setpoint - measured_value
      integral = integral + error*dt
      derivative = (error - previous_error)/dt
      output = Kp*error + Ki*integral + Kd*derivative
      previous_error = error
      wait(dt)
      goto start
    '''
    def __init__(self, Kp=2., Ki=0., Kd=1.,\
                 set_point=None, output_limits=None,\
                 max_age=None):
        self.Kp = Kp
        self.Ki = Ki
        self.Kd = Kd
        self.Pval = None
        self.Ival = 0.0
        self.Dval = 0.0
        self.previous_error = None
        self.set_point = None
        if set_point: self.set_point = set_point
        self.output_limits = output_limits
        self.history = []
        self.max_age = max_age


    def recalculate(self, value, dt=1.0, new_set_point=None):
        if new_set_point:
            self.set_point = float(new_set_point)

        ## Pval
        error = self.set_point - value
        self.Pval = error

        ## Ival
        for entry in self.history:
            entry[2] += dt
        for entry in self.history:
            if self.max_age:
                if entry[2] > self.max_age:
                    self.history.remove(entry)
        self.history.append([error, dt, 0])
        new_Ival = 0
        for entry in self.history:
            new_Ival += entry[0]*entry[1]
        self.Ival = new_Ival
#         self.Ival = self.Ival + error*dt

        ## Dval
        if self.previous_error:
            self.Dval = (error - self.previous_error)/dt

        ## Output
        output = self.Kp*error + self.Ki*self.Ival + self.Kd*self.Dval
        if self.output_limits:
            if output > max(self.output_limits): output = max(self.output_limits)
            if output < min(self.output_limits): output = min(self.output_limits)
        self.previous_error = error
        return output


    def tune(self, Kp=None, Ki=None, Kd=None):
        if Kp: self.Kp = Kp
        if Ki: self.Ki = Ki
        if Kd: self.Kd = Kd


##-----------------------------------------------------------------------------
## AAG Cloud Sensor Class
##-----------------------------------------------------------------------------
class AAGCloudSensor(WeatherStation):
    '''
    This class is for the AAG Cloud Sensor device which can be communicated with
    via serial commands.

    http://www.aagware.eu/aag/cloudwatcherNetwork/TechInfo/Rs232_Comms_v100.pdf
    http://www.aagware.eu/aag/cloudwatcherNetwork/TechInfo/Rs232_Comms_v110.pdf
    http://www.aagware.eu/aag/cloudwatcherNetwork/TechInfo/Rs232_Comms_v120.pdf

    Command List (from Rs232_Comms_v100.pdf)
    !A = Get internal name (recieves 2 blocks)
    !B = Get firmware version (recieves 2 blocks)
    !C = Get values (recieves 5 blocks)
         Zener voltage, Ambient Temperature, Ambient Temperature, Rain Sensor Temperature, HSB
    !D = Get internal errors (recieves 5 blocks)
    !E = Get rain frequency (recieves 2 blocks)
    !F = Get switch status (recieves 2 blocks)
    !G = Set switch open (recieves 2 blocks)
    !H = Set switch closed (recieves 2 blocks)
    !Pxxxx = Set PWM value to xxxx (recieves 2 blocks)
    !Q = Get PWM value (recieves 2 blocks)
    !S = Get sky IR temperature (recieves 2 blocks)
    !T = Get sensor temperature (recieves 2 blocks)
    !z = Reset RS232 buffer pointers (recieves 1 blocks)
    !K = Get serial number (recieves 2 blocks)

    Return Codes
    '1 '    Infra red temperature in hundredth of degree Celsius
    '2 '    Infra red sensor temperature in hundredth of degree Celsius
    '3 '    Analog0 output 0-1023 => 0 to full voltage (Ambient Temp NTC)
    '4 '    Analog2 output 0-1023 => 0 to full voltage (LDR ambient light)
    '5 '    Analog3 output 0-1023 => 0 to full voltage (Rain Sensor Temp NTC)
    '6 '    Analog3 output 0-1023 => 0 to full voltage (Zener Voltage reference)
    'E1'    Number of internal errors reading infra red sensor: 1st address byte
    'E2'    Number of internal errors reading infra red sensor: command byte
    'E3'    Number of internal errors reading infra red sensor: 2nd address byte
    'E4'    Number of internal errors reading infra red sensor: PEC byte NB: the error
            counters are reset after being read.
    'N '    Internal Name
    'V '    Firmware Version number
    'Q '    PWM duty cycle
    'R '    Rain frequency counter
    'X '    Switch Opened
    'Y '    Switch Closed

    Advice from the manual:

    * When communicating with the device send one command at a time and wait for
    the respective reply, checking that the correct number of characters has
    been received.

    * Perform more than one single reading (say, 5) and apply a statistical
    analysis to the values to exclude any outlier.

    * The rain frequency measurement is the one that takes more time - 280 ms

    * The following reading cycle takes just less than 3 seconds to perform:
        * Perform 5 times:
            * get IR temperature
            * get Ambient temperature
            * get Values
            * get Rain Frequency
        * get PWM value
        * get IR errors
        * get SWITCH Status

    '''

    def __init__(self, serial_address=None):
        super().__init__()

        ## Make logger
        logger = logging.getLogger('AAG_cloud_sensor')
        if len(logger.handlers) == 0:
            logger.setLevel(logging.DEBUG)
            ## Set up console output
            LogConsoleHandler = logging.StreamHandler()
            LogConsoleHandler.setLevel(logging.INFO)
            LogFormat = logging.Formatter('%(asctime)23s %(levelname)8s: %(message)s')
            LogConsoleHandler.setFormatter(LogFormat)
            logger.addHandler(LogConsoleHandler)
            ## Set up file output
            LogFilePath = os.path.join('/', 'var', 'panoptes', 'logs', 'PanoptesWeather')
            if not os.path.exists(LogFilePath):
                os.mkdir(LogFilePath)
            now = dt.utcnow()
            LogFileName = now.strftime('AAGCloudSensor.log')
            LogFile = os.path.join(LogFilePath, LogFileName)
    #         LogFileHandler = logging.FileHandler(LogFile)
            LogFileHandler = logging.handlers.TimedRotatingFileHandler(LogFile,\
                                     when='midnight', interval=1, utc=True)
            LogFileHandler.setLevel(logging.DEBUG)
            LogFileHandler.setFormatter(LogFormat)
            logger.addHandler(LogFileHandler)
        self.logger = logger

        ## Read configuration
        self.cfg = load_config()['weather']['aag_cloud']

        ## Initialize Serial Connection
        if not serial_address:
            if 'serial_port' in self.cfg.keys():
                serial_address = self.cfg['serial_port']
            else:
                serial_address = '/dev/ttyUSB0'
        if self.logger: self.logger.debug('Using serial address: {}'.format(serial_address))
        if serial_address:
            if self.logger: self.logger.info('Connecting to AAG Cloud Sensor')
            try:
                self.AAG = serial.Serial(serial_address, 9600, timeout=2)
                if self.logger: self.logger.info("  Connected to Cloud Sensor on {}".format(serial_address))
            except OSError as e:
                if self.logger: self.logger.error('Unable to connect to AAG Cloud Sensor')
                if self.logger: self.logger.error('  {}'.format(e.errno))
                if self.logger: self.logger.error('  {}'.format(e.strerror))
                self.AAG = None
            except:
                if self.logger: self.logger.error("Unable to connect to AAG Cloud Sensor")
                self.AAG = None
        else:
            self.AAG = None
        ## Thresholds

        ## Initialize Values
        self.last_update = None
        self.safe = None
        self.ambient_temp = None
        self.sky_temp = None
        self.wind_speed = None
        self.internal_voltage = None
        self.LDR_resistance = None
        self.rain_sensor_temp = None
        self.PWM = None
        self.errors = None
        self.switch = None
        self.safe_dict = None
        self.hibernate = 0.500  ## time to wait after failed query
        ## Set Up Heater
        if 'heater' in self.cfg.keys():
            self.heater_cfg = self.cfg['heater']
        else:
            self.heater_cfg = {
                               'low_temp': 0,
                               'low_delta': 6,
                               'high_temp': 20,
                               'high_delta': 4,
                               'min_power': 10,
                               'impulse_temp': 10,
                               'impulse_duration': 60,
                               'impulse_cycle': 600,
                               }
        self.heater_PID = PID(Kp=3.0, Ki=1.0, Kd=1.0,\
                              max_age=20,\
                              output_limits=[self.heater_cfg['min_power'],100])
        self.impulse_heating = None
        self.impulse_start = None

        ## Command Translation
        self.commands = {'!A': 'Get internal name',
                         '!B': 'Get firmware version',
                         '!C': 'Get values',
                         '!D': 'Get internal errors',
                         '!E': 'Get rain frequency',
                         '!F': 'Get switch status',
                         '!G': 'Set switch open',
                         '!H': 'Set switch closed',
                         'P\d\d\d\d!': 'Set PWM value',
                         '!Q': 'Get PWM value',
                         '!S': 'Get sky IR temperature',
                         '!T': 'Get sensor temperature',
                         '!z': 'Reset RS232 buffer pointers',
                         '!K': 'Get serial number',
                         'v!': 'Query if anemometer enabled',
                         'V!': 'Get wind speed',
                         'M!': 'Get electrical constants',
                         '!Pxxxx': 'Set PWM value to xxxx',
                         }
        self.expects = {'!A': '!N\s+(\w+)!',
                        '!B': '!V\s+([\d\.\-]+)!',
                        '!C': '!6\s+([\d\.\-]+)!4\s+([\d\.\-]+)!5\s+([\d\.\-]+)!',
                        '!D': '!E1\s+([\d\.]+)!E2\s+([\d\.]+)!E3\s+([\d\.]+)!E4\s+([\d\.]+)!',
                        '!E': '!R\s+([\d\.\-]+)!',
                        '!F': '!Y\s+([\d\.\-]+)!',
                        'P\d\d\d\d!': '!Q\s+([\d\.\-]+)!',
                        '!Q': '!Q\s+([\d\.\-]+)!',
                        '!S': '!1\s+([\d\.\-]+)!',
                        '!T': '!2\s+([\d\.\-]+)!',
                        '!K': '!K(\d+)\s*\\x00!',
                        'v!': '!v\s+([\d\.\-]+)!',
                        'V!': '!w\s+([\d\.\-]+)!',
                        'M!': '!M(.{12})',
                        }
        self.delays = {\
                       '!E': 0.350,
                       }
        if self.AAG:
            ## Query Device Name
            result = self.query('!A')
            if result:
                self.name = result[0].strip()
                if self.logger: self.logger.info('  Device Name is "{}"'.format(self.name))
            else:
                self.name = ''
                if self.logger: self.logger.warning('  Failed to get Device Name')
                sys.exit(1)

            ## Query Firmware Version
            result = self.query('!B')
            if result:
                self.firmware_version = result[0].strip()
                if self.logger: self.logger.info('  Firmware Version = {}'.format(self.firmware_version))
            else:
                self.firmware_version = ''
                if self.logger: self.logger.warning('  Failed to get Firmware Version')
                sys.exit(1)

            ## Query Serial Number
            result = self.query('!K')
            if result:
                self.serial_number = result[0].strip()
                if self.logger: self.logger.info('  Serial Number: {}'.format(self.serial_number))
            else:
                self.serial_number = ''
                if self.logger: self.logger.warning('  Failed to get Serial Number')
                sys.exit(1)


    def send(self, send, delay=0.100):

        found_command = False
        for cmd in self.commands.keys():
            if re.match(cmd, send):
                if self.logger: self.logger.debug('Sending command: {}'.format(self.commands[cmd]))
                found_command = True
                break
        if not found_command:
            if self.logger: self.logger.warning('Unknown command: "{}"'.format(send))
            return None

        if self.logger: self.logger.debug('  Clearing buffer')
        cleared = self.AAG.read(self.AAG.inWaiting())
        if len(cleared) > 0:
            if self.logger: self.logger.debug('  Cleared: "{}"'.format(cleared.decode('utf-8')))

        self.AAG.write(send.encode('utf-8'))
        time.sleep(delay)
        response = self.AAG.read(self.AAG.inWaiting()).decode('utf-8')
        if self.logger: self.logger.debug('  Response: "{}"'.format(response))
        ResponseMatch = re.match('(!.*)\\x11\s{12}0', response)
        if ResponseMatch:
            result = ResponseMatch.group(1)
        else:
            result = response

        return result


    def query(self, send, maxtries=5):
        found_command = False
        for cmd in self.commands.keys():
            if re.match(cmd, send):
                if self.logger: self.logger.debug('Sending command: {}'.format(self.commands[cmd]))
                found_command = True
                break
        if not found_command:
            if self.logger: self.logger.warning('Unknown command: "{}"'.format(send))
            return None

        if cmd in self.delays.keys():
            delay = self.delays[cmd]
        else:
            delay = 0.200
        expect = self.expects[cmd]
        count = 0
        result = None
        while not result and (count <= maxtries):
            count += 1
            result = self.send(send, delay=delay)

            MatchExpect = re.match(expect, result)
            if not MatchExpect:
                if self.logger: self.logger.debug('Did not find {} in response "{}"'.format(expect, result))
                result = None
                time.sleep(self.hibernate)
            else:
                if self.logger: self.logger.debug('Found {} in response "{}"'.format(expect, result))
                result = MatchExpect.groups()
        return result


    def get_ambient_temperature(self, n=5):
        '''
        Populates the self.ambient_temp property

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" item 5.
        '''
        if self.logger: self.logger.info('Getting ambient temperature')
        values = []
        for i in range(0,n):
            try:
                value = float(self.query('!T')[0])/100.
            except:
                pass
            else:
                if self.logger: self.logger.debug('  Ambient Temperature Query = {:.1f}'.format(value))
                values.append(value)
        if len(values) >= n-1:
            self.ambient_temp = np.median(values)*u.Celsius
            if self.logger: self.logger.info('  Ambient Temperature = {:.1f}'.format(self.ambient_temp))
        else:
            self.ambient_temp = None
            if self.logger: self.logger.info('  Failed to Read Ambient Temperature')
        return self.ambient_temp


    def get_sky_temperature(self, n=9):
        '''
        Populates the self.sky_temp property

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" item 1.

        Does this n times as recommended by the "Communication operational
        recommendations" section in Rs232_Comms_v100.pdf
        '''
        if self.logger: self.logger.info('Getting sky temperature')
        values = []
        for i in range(0,n):
            try:
                value = float(self.query('!S')[0])/100.
            except:
                pass
            else:
                if self.logger: self.logger.debug('  Sky Temperature Query = {:.1f}'.format(value))
                values.append(value)
        if len(values) >= n-1:
            self.sky_temp = np.median(values)*u.Celsius
            if self.logger: self.logger.info('  Sky Temperature = {:.1f}'.format(self.sky_temp))
        else:
            self.sky_temp = None
            if self.logger: self.logger.info('  Failed to Read Sky Temperature')
        return self.sky_temp


    def get_values(self, n=5):
        '''
        Populates the self.internal_voltage, self.LDR_resistance, and
        self.rain_sensor_temp properties

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" items 4, 6, 7.
        '''
        if self.logger: self.logger.info('Getting "values"')
        ZenerConstant = 3
        LDRPullupResistance = 56.
        RainPullUpResistance = 1
        RainResAt25 = 1
        RainBeta = 3450.
        ABSZERO = 273.15
        internal_voltages = []
        LDR_resistances = []
        rain_sensor_temps = []
        for i in range(0,n):
            responses = self.query('!C')
            try:
                internal_voltage = 1023 * ZenerConstant / float(responses[0])
                internal_voltages.append(internal_voltage)
                LDR_resistance = LDRPullupResistance / ((1023. / float(responses[1])) - 1.)
                LDR_resistances.append(LDR_resistance)
                r = np.log(RainPullUpResistance / ((1023. / float(responses[2])) - 1.) / RainResAt25)
                rain_sensor_temp = 1. / (r / RainBeta + 1. / (ABSZERO + 25.)) - ABSZERO
                rain_sensor_temps.append(rain_sensor_temp)
            except:
                pass

        ## Median Results
        if len(internal_voltages) >= n-1:
            self.internal_voltage = np.median(internal_voltages) * u.volt
            if self.logger: self.logger.info('  Internal Voltage = {:.2f}'.format(self.internal_voltage))
        else:
            self.internal_voltage = None
            if self.logger: self.logger.info('  Failed to read Internal Voltage')

        if len(LDR_resistances) >= n-1:
            self.LDR_resistance = np.median(LDR_resistances) * u.kohm
            if self.logger: self.logger.info('  LDR Resistance = {:.0f}'.format(self.LDR_resistance))
        else:
            self.LDR_resistance = None
            if self.logger: self.logger.info('  Failed to read LDR Resistance')

        if len(rain_sensor_temps) >= n-1:
            self.rain_sensor_temp = np.median(rain_sensor_temps) * u.Celsius
            if self.logger: self.logger.info('  Rain Sensor Temp = {:.1f}'.format(self.rain_sensor_temp))
        else:
            self.rain_sensor_temp = None
            if self.logger: self.logger.info('  Failed to read Rain Sensor Temp')

        return (self.internal_voltage, self.LDR_resistance, self.rain_sensor_temp)


    def get_rain_frequency(self, n=5):
        '''
        Populates the self.rain_frequency property
        '''
        if self.logger: self.logger.info('Getting rain frequency')
        values = []
        for i in range(0,n):
            try:
                value = float(self.query('!E')[0]) * 100. / 1023.
                if self.logger: self.logger.debug('  Rain Freq Query = {:.1f}'.format(value))
                values.append(value)
            except:
                pass
        if len(values) >= n-1:
            self.rain_frequency = np.median(values)
            if self.logger: self.logger.info('  Rain Frequency = {:.1f}'.format(self.rain_frequency))
        else:
            self.rain_frequency = None
            if self.logger: self.logger.info('  Failed to read Rain Frequency')
        return self.rain_frequency


    def get_PWM(self):
        '''
        Populates the self.PWM property.

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" item 3.
        '''
        if self.logger: self.logger.info('Getting PWM value')
        try:
            value = self.query('!Q')[0]
            self.PWM = float(value) * 100. / 1023.
            if self.logger: self.logger.info('  PWM Value = {:.1f}'.format(self.PWM))
        except:
            self.PWM = None
            if self.logger: self.logger.info('  Failed to read PWM Value')
        return self.PWM


    def set_PWM(self, percent):
        '''
        '''
        if percent < 0.: percent = 0.
        if percent > 100.: percent = 100.
        if self.logger: self.logger.info('Setting PWM value to {:.1f} %'.format(percent))
        send_digital = int(1023. * float(percent) / 100.)
        send_string = 'P{:04d}!'.format(send_digital)
        result = self.query(send_string)
        if result:
            self.PWM = float(result[0]) * 100. / 1023.
            if self.logger: self.logger.info('  PWM Value = {:.1f}'.format(self.PWM))


    def get_errors(self):
        '''
        Populates the self.IR_errors property
        '''
        if self.logger: self.logger.info('Getting errors')
        response = self.query('!D')
        if response:
            self.errors = {'!E1': str(int(response[0])),
                           '!E2': str(int(response[1])),
                           '!E3': str(int(response[2])),
                           '!E4': str(int(response[3])) }
            if self.logger: self.logger.info("  Internal Errors: {} {} {} {}".format(\
                             self.errors['!E1'],\
                             self.errors['!E2'],\
                             self.errors['!E3'],\
                             self.errors['!E4'],\
                             ))

        else:
            self.errors = {'!E1': None,
                           '!E2': None,
                           '!E3': None,
                           '!E4': None }
        return self.errors


    def get_switch(self, maxtries=3):
        '''
        Populates the self.switch property

        Unlike other queries, this method has to check if the return matches a
        !X or !Y pattern (indicating open and closed respectively) rather than
        read a value.
        '''
        if self.logger: self.logger.info('Getting switch status')
        self.switch = None
        tries = 0
        status = None
        while not status:
            tries += 1
            response = self.send('!F')
            if re.match('!Y            1!', response):
                status = 'OPEN'
            elif re.match('!X            1!', response):
                status = 'CLOSED'
            else:
                status = None
            if not status and tries >= maxtries:
                status = 'UNKNOWN'
        self.switch = status
        if self.logger: self.logger.info('  Switch Status = {}'.format(self.switch))
        return self.switch


    def wind_speed_enabled(self):
        '''
        Method returns true or false depending on whether the device supports
        wind speed measurements.
        '''
        if self.logger: self.logger.debug('Checking if wind speed is enabled')
        try:
            enabled = bool(self.query('v!')[0])
            if enabled:
                if self.logger: self.logger.debug('  Anemometer enabled')
            else:
                if self.logger: self.logger.debug('  Anemometer not enabled')
        except:
            enabled = None
        return enabled


    def get_wind_speed(self, n=3):
        '''
        Populates the self.wind_speed property

        Based on the information in Rs232_Comms_v120.pdf document

        Medians n measurements.  This isn't mentioned specifically by the manual
        but I'm guessing it won't hurt.
        '''
        if self.logger: self.logger.info('Getting wind speed')
        if self.wind_speed_enabled():
            values = []
            for i in range(0,n):
                result = self.query('V!')
                if result:
                    value = float(result[0])
                    if self.logger: self.logger.debug('  Wind Speed Query = {:.1f}'.format(value))
                    values.append(value)
            if len(values) >= 3:
                self.wind_speed = np.median(values)*u.km/u.hr
                if self.logger: self.logger.info('  Wind speed = {:.1f}'.format(self.wind_speed))
            else:
                self.wind_speed = None
        else:
            self.wind_speed = None
        return self.wind_speed


    def update_weather(self, update_mongo=True):
        '''
        '''
        data = {}
        data['Device Name'] = self.name
        data['Firmware Version'] = self.firmware_version
        data['Device Serial Number'] = self.serial_number
        if self.get_sky_temperature():
            data['Sky Temperature (C)'] = self.sky_temp.value
        if self.get_ambient_temperature():
            data['Ambient Temperature (C)'] = self.ambient_temp.value
        self.get_values()
        if self.internal_voltage:
            data['Internal Voltage (V)'] = self.internal_voltage.value
        if self.LDR_resistance:
            data['LDR Resistance (ohm)'] = self.LDR_resistance.value
        if self.rain_sensor_temp:
            data['Rain Sensor Temp (C)'] = self.rain_sensor_temp.value
        if self.get_rain_frequency():
            data['Rain Frequency'] = self.rain_frequency
        if self.get_PWM():
            data['PWM Value'] = self.PWM
        if self.get_errors():
            data['Errors'] = self.errors
#         if self.get_switch():
#             data['Switch Status'] = self.switch
        if self.get_wind_speed():
            data['Wind Speed (km/h)'] = self.wind_speed.value
        ## Make Safety Decision
        self.safe_dict = make_safety_decision(self.cfg)
        data['Safe'] = self.safe_dict['Safe']
        data['Sky Safe'] = self.safe_dict['Sky']
        data['Wind Safe'] = self.safe_dict['Wind']
        data['Gust Safe'] = self.safe_dict['Gust']
        data['Rain Safe'] = self.safe_dict['Rain']

        if update_mongo:
            try:
                # Connect to sensors collection
                sensors = PanMongo().sensors
                if self.logger: self.logger.info('Connected to mongo')
                sensors.insert({
                    "date": dt.utcnow(),
                    "type": "weather",
                    "data": data
                })
                if self.logger: self.logger.info('  Inserted mongo document')
                sensors.update({"status": "current", "type": "weather"},\
                               {"$set": {\
                                   "date": dt.utcnow(),\
                                   "type": "weather",\
                                   "data": data,\
                               }},\
                               True)
                if self.logger: self.logger.info('  Updated current status document')
            except:
                if self.logger: self.logger.warning('Failed to update mongo database')
        else:
            print('{:>26s}: {}'.format('Date and Time',\
                   dt.utcnow().strftime('%Y/%m/%d %H:%M:%S')))
            for key in ['Ambient Temperature (C)', 'Sky Temperature (C)',\
                        'PWM Value', 'Rain Frequency', 'Safe']:
                if key in data.keys():
                    print('{:>26s}: {}'.format(key, data[key]))
                else:
                    print('{:>26s}: {}'.format(key, 'no data'))
            print('')

        return self.safe


#     def heater_algorithm(self, target, last_entry):
#         '''
#         Uses the algorithm described in RainSensorHeaterAlgorithm.pdf to
#         determine PWM value.
#         
#         Values are for the default read cycle of 10 seconds.
#         '''
#         deltaT = last_entry['Rain Sensor Temp (C)'] - target
#         scaling = 0.5
#         if deltaT > 8.:
#             deltaPWM = -40*scaling
#         elif deltaT > 4.:
#             deltaPWM = -20*scaling
#         elif deltaT > 3.:
#             deltaPWM = -10*scaling
#         elif deltaT > 2.:
#             deltaPWM = -6*scaling
#         elif deltaT > 1.:
#             deltaPWM = -4*scaling
#         elif deltaT > 0.5:
#             deltaPWM = -2*scaling
#         elif deltaT > 0.3:
#             deltaPWM = -1*scaling
#         elif deltaT < -0.3:
#             deltaPWM = 1*scaling
#         elif deltaT < -0.5:
#             deltaPWM = 2*scaling
#         elif deltaT < -1.:
#             deltaPWM = 4*scaling
#         elif deltaT < -2.:
#             deltaPWM = 6*scaling
#         elif deltaT < -3.:
#             deltaPWM = 10*scaling
#         elif deltaT < -4.:
#             deltaPWM = 20*scaling
#         elif deltaT < -8.:
#             deltaPWM = 40*scaling
#         return int(deltaPWM)


    def calculate_and_set_PWM(self):
        '''
        Uses the algorithm described in RainSensorHeaterAlgorithm.pdf to decide
        whether to use impulse heating mode, then determines the correct PWM
        value.
        '''
        self.logger.info('Calculating new PWM Value')
        ## Get Last n minutes of rain history
        now = dt.utcnow()
        start = now - tdelta(0, int(self.heater_cfg['impulse_cycle']))
        sensors = PanMongo().sensors
        entries = [x for x\
                   in sensors.find( {"type" : "weather", 'date': {'$gt': start, '$lt': now} } )\
                   ]
        self.logger.info('  Found {} entries in last {:d} seconds.'.format(\
                         len(entries), int(self.heater_cfg['impulse_cycle']),
                        ))
        last_entry = [x for x\
                      in sensors.find( {"type" : "weather", 'status':'current'} )\
                     ][0]['data']
        rain_history = [x['data']['Rain Safe']\
                        for x\
                        in entries
                        if 'Rain Safe' in x['data'].keys()\
                        ]

        if not 'Ambient Temperature (C)' in last_entry.keys():
            self.logger.warning('  Do not have Ambient Temperature measurement.  Can not determine PWM value.')
        elif not 'Rain Sensor Temp (C)' in last_entry.keys():
            self.logger.warning('  Do not have Rain Sensor Temperature measurement.  Can not determine PWM value.')
        else:
            ## Decide whether to use the impulse heating mechanism
            if len(rain_history) > 3 and not np.any(rain_history):
                self.logger.info('  Consistent wet/rain in history.  Using impulse heating.')
                if self.impulse_heating:
                    impulse_time = (now - self.impulse_start).total_seconds()
                    if impulse_time > float(self.heater_cfg['impulse_duration']):
                        self.logger.info('  Impulse heating has been on for > {:.0f} seconds.  Turning off.'.format(\
                                         float(self.heater_cfg['impulse_duration'])
                                        ))
                        self.impulse_heating = False
                        self.impulse_start = None
                    else:
                        self.logger.info('  Impulse heating has been on for {:.0f} seconds.'.format(\
                                         impulse_time))
                else:
                    self.logger.info('  Starting impulse heating sequence.')
                    self.impulse_start = now
                    self.impulse_heating = True
            else:
                self.logger.info('  No impulse heating needed.')
                self.impulse_heating = False
                self.impulse_start = None

            ## Set PWM Based on Impulse Method or Normal Method
            if self.impulse_heating:
                target_temp = float(last_entry['Ambient Temperature (C)']) + float(self.heater_cfg['impulse_temp'])
                if last_entry['Rain Sensor Temp (C)'] < target_temp:
                    self.logger.info('  Rain sensor temp < target.  Setting heater to 100 %.')
                    self.set_PWM(100)
                else:
                    new_PWM = int(self.heater_PID.recalculate(last_entry['Rain Sensor Temp (C)'],\
                                                          new_set_point=target_temp))
                    self.logger.info('  target temp = {:.1f}, actual = {:.1f}'.format(\
                                     target_temp, last_entry['Rain Sensor Temp (C)']))
                    self.logger.info('  new_value = {:.0f}, P = {:.0f}, I = {:.0f}, D = {:.0f}'.format(\
                                      new_PWM, self.heater_PID.Kp*self.heater_PID.Pval,\
                                      self.heater_PID.Ki*self.heater_PID.Ival,\
                                      self.heater_PID.Kd*self.heater_PID.Dval))
                    self.logger.info('  Rain sensor temp > target.  Setting heater to {:d} %.'.format(new_PWM))
                    self.set_PWM(new_PWM)
            else:
                if last_entry['Ambient Temperature (C)'] < self.heater_cfg['low_temp']:
                    deltaT = self.heater_cfg['low_delta']
                elif last_entry['Ambient Temperature (C)'] > self.heater_cfg['high_temp']:
                    deltaT = self.heater_cfg['high_delta']
                else:
                    frac = (last_entry['Ambient Temperature (C)'] - self.heater_cfg['low_temp']) /\
                           (self.heater_cfg['high_temp'] - self.heater_cfg['low_temp'])
                    deltaT = self.heater_cfg['low_delta'] + frac*(self.heater_cfg['high_delta']-self.heater_cfg['low_delta'])
                target_temp = last_entry['Ambient Temperature (C)'] + deltaT
                new_PWM = int(self.heater_PID.recalculate(last_entry['Rain Sensor Temp (C)'],\
                                                      new_set_point=target_temp))
                self.logger.info('  target temp = {:.1f}, actual = {:.1f}'.format(\
                                 target_temp, last_entry['Rain Sensor Temp (C)']))
                self.logger.info('  new_value = {:.0f}, P = {:.0f}, I = {:.0f}, D = {:.0f}'.format(\
                                  new_PWM, self.heater_PID.Kp*self.heater_PID.Pval,\
                                  self.heater_PID.Ki*self.heater_PID.Ival,\
                                  self.heater_PID.Kd*self.heater_PID.Dval))
                self.logger.info('  Setting heater to {:d} %.'.format(new_PWM))
                self.set_PWM(new_PWM)


def make_safety_decision(cfg):
    '''
    Method makes decision whether conditions are safe or unsafe.
    '''
    ## If sky-amb > threshold, then cloudy (safe)
    if 'threshold_cloudy' in cfg.keys():
        threshold_cloudy = cfg['threshold_cloudy']
    else:
        threshold_cloudy = -20
    ## If sky-amb > threshold, then very cloudy (unsafe)
    if 'threshold_very_cloudy' in cfg.keys():
        threshold_very_cloudy = cfg['threshold_very_cloudy']
    else:
        threshold_very_cloudy = -15
    ## If avg_wind > threshold, then windy (safe)
    if 'threshold_windy' in cfg.keys():
        threshold_windy = cfg['threshold_windy']
    else:
        threshold_windy = 20
    ## If avg_wind > threshold, then very windy (unsafe)
    if 'threshold_very_windy' in cfg.keys():
        threshold_very_windy = cfg['threshold_very_windy']
    else:
        threshold_very_windy = 30
    ## If wind > threshold, then gusty (safe)
    if 'threshold_gusty' in cfg.keys():
        threshold_gusty = cfg['threshold_gusty']
    else:
        threshold_gusty = 40
    ## If wind > threshold, then very gusty (unsafe)
    if 'threshold_very_gusty' in cfg.keys():
        threshold_very_gusty = cfg['threshold_very_gusty']
    else:
        threshold_very_gusty = 50
    ## If rain frequency < threshold, then unsafe
    if 'threshold_rainy' in cfg.keys():
        threshold_rain = cfg['threshold_rainy']
    else:
        threshold_rain = 230

    ## Get Last n minutes of data
    if 'safety_delay' in cfg.keys():
        safety_delay = cfg['safety_delay']
    else:
        safety_delay = 15.
    end = dt.utcnow()
    start = end - tdelta(0, int(safety_delay*60))
    sensors = PanMongo().sensors
    entries = [x for x in sensors.find( {"type" : "weather", 'date': {'$gt': start, '$lt': end} } )]
    print('Found {} weather data entries in last {:.0f} minutes'.format(len(entries), safety_delay))

    ## Cloudiness
    sky_diff = [x['data']['Sky Temperature (C)'] - x['data']['Ambient Temperature (C)']\
                for x in entries\
                if 'Ambient Temperature (C)' in x['data'].keys()\
                and 'Sky Temperature (C)' in x['data'].keys()]
    if len(sky_diff) == 0:
        sky_safe = False
    elif max(sky_diff) < threshold_very_cloudy:
        sky_safe = True
    else:
        sky_safe = False

    if len(sky_diff) == 0:
        sky_now_safe = False
    elif sky_diff[-1] < threshold_very_cloudy:
        sky_now_safe = True
    else:
        sky_now_safe = False

    ## Wind (average and gusts)
    wind_speed = [x['data']['Wind Speed (km/h)']\
                  for x in entries\
                  if 'Wind Speed (km/h)' in x['data'].keys()]

    if len(wind_speed) == 0:
        wind_safe = False
        gust_safe = False
        wind_now_safe = False
        gust_now_safe = False
    else:
        typical_data_interval = (end - min([x['date'] for x in entries])).total_seconds()/len(entries)
        mavg_count = int(np.ceil(120./typical_data_interval))
        wind_mavg = movingaverage(wind_speed, mavg_count)
        if max(wind_mavg) > threshold_very_windy:
            wind_safe = False
        else:
            wind_safe = True
        if wind_mavg[-1] > threshold_very_windy:
            wind_now_safe = False
        else:
            wind_now_safe = True
        if max(wind_speed) > threshold_very_gusty:
            gust_safe = False
        else:
            gust_safe = True
        if wind_speed[-1] > threshold_very_gusty:
            gust_now_safe = False
        else:
            gust_now_safe = True

    ## Rain
    rf_value = [x['data']['Rain Frequency']\
                  for x in entries\
                  if 'Rain Frequency' in x['data'].keys()]

    if len(rf_value) == 0:
        rain_safe = False
    elif min(rf_value) < threshold_rain:
        rain_safe = False
    else:
        rain_safe = True
    if len(rf_value) == 0:
        rain_now_safe = False
    elif rf_value[-1] < threshold_rain:
        rain_now_safe = False
    else:
        rain_now_safe = True

    safe = sky_safe & wind_safe & gust_safe & rain_safe
    translator = {True: 'safe', False: 'unsafe'}
    if safe:
        print('Safe (Sky: {}, Wind: {}, Gust: {}, Rain: {})'.format(\
              translator[sky_now_safe], translator[wind_now_safe],\
              translator[gust_now_safe], translator[rain_now_safe]))
    else:
        print('Unsafe (Sky: {}, Wind: {}, Gust: {}, Rain: {})'.format(\
              translator[sky_now_safe], translator[wind_now_safe],\
              translator[gust_now_safe], translator[rain_now_safe]))

    safe_dict = {'Safe': safe,
                 'Sky': sky_now_safe,
                 'Wind': wind_now_safe,
                 'Gust': gust_now_safe,
                 'Rain': rain_now_safe}
    return safe_dict


def plot_weather(date_string):
    import matplotlib as mpl
    mpl.use('Agg')
    from matplotlib import pyplot as plt
    from matplotlib.dates import HourLocator, MinuteLocator, DateFormatter
    plt.ioff()

    dpi=100
    Figure = plt.figure(figsize=(16,9), dpi=dpi)
    hours = HourLocator(byhour=range(25))
    hours_fmt = DateFormatter('%H')

    if not date_string:
        today = True
        date = dt.utcnow()
        date_string = date.strftime('%Y%m%dUT')
    else:
        today = False
        date = dt.strptime('{} 23:59:59'.format(date_string), '%Y%m%dUT %H:%M:%S')
    start = dt(date.year, date.month, date.day, 0, 0, 0, 0)
    end = dt(date.year, date.month, date.day, 23, 59, 59, 0)

    ##------------------------------------------------------------------------
    ## Use pyephem determine sunrise and sunset times
    ##------------------------------------------------------------------------
    pan = panoptes.Panoptes()
    obs = pan.observatory

    sunset = obs.sun_set_time(Time(start), which='next').datetime
    evening_civil_twilight = obs.twilight_evening_civil(Time(start), which='next').datetime
    evening_nautical_twilight = obs.twilight_evening_nautical(Time(start), which='next').datetime
    evening_astronomical_twilight = obs.twilight_evening_astronomical(Time(start), which='next').datetime
    morning_astronomical_twilight = obs.twilight_morning_astronomical(Time(start), which='next').datetime
    morning_nautical_twilight = obs.twilight_morning_nautical(Time(start), which='next').datetime
    morning_civil_twilight = obs.twilight_morning_civil(Time(start), which='next').datetime
    sunrise = obs.sun_rise_time(Time(start), which='next').datetime

#     print('start:                         {}'.format(Time(start)))
#     print(obs.is_night(Time(start)))
#     print('sunset:                        {}'.format(sunset))
#     print('evening_civil_twilight:        {}'.format(evening_civil_twilight))
#     print('evening_nautical_twilight:     {}'.format(evening_nautical_twilight))
#     print('evening_astronomical_twilight: {}'.format(evening_astronomical_twilight))
#     print('morning_astronomical_twilight: {}'.format(morning_astronomical_twilight))
#     print('morning_nautical_twilight:     {}'.format(morning_nautical_twilight))
#     print('morning_civil_twilight:        {}'.format(morning_civil_twilight))

    ##-------------------------------------------------------------------------
    ## Plot a day's weather
    ##-------------------------------------------------------------------------
    plot_positions = [ ( [0.000, 0.835, 0.700, 0.170], [0.720, 0.835, 0.280, 0.170] ),
                       ( [0.000, 0.635, 0.700, 0.170], [0.720, 0.635, 0.280, 0.170] ),
                       ( [0.000, 0.450, 0.700, 0.170], [0.720, 0.450, 0.280, 0.170] ),
                       ( [0.000, 0.265, 0.700, 0.170], [0.720, 0.265, 0.280, 0.170] ),
                       ( [0.000, 0.185, 0.700, 0.065], [0.720, 0.185, 0.280, 0.065] ),
                       ( [0.000, 0.000, 0.700, 0.170], [0.740, 0.000, 0.260, 0.170] ),
                     ]

    # Connect to sensors collection
    sensors = PanMongo().sensors
    entries = [x for x in sensors.find( {"type" : "weather", 'date': {'$gt': start, '$lt': end} } )]

    ##-------------------------------------------------------------------------
    ## Plot Ambient Temperature vs. Time
    t_axes = plt.axes(plot_positions[0][0])
    if today:
        time_title = date
    else:
        time_title = end
    plt.title('Weather for {} at {}'.format(date_string, time_title.strftime('%H:%M:%S UT')))
    amb_temp = [x['data']['Ambient Temperature (C)']\
                for x in entries\
                if 'Ambient Temperature (C)' in x['data'].keys()]
    time = [x['date'] for x in entries\
                if 'Ambient Temperature (C)' in x['data'].keys()]
    t_axes.plot_date(time, amb_temp, 'ko',\
                     markersize=2, markeredgewidth=0,\
                     drawstyle="default")
    plt.ylabel("Ambient Temp. (C)")
    plt.grid(which='major', color='k')
    plt.yticks(range(-100,100,10))
    plt.xlim(start, end)
    plt.ylim(-5,35)
    t_axes.xaxis.set_major_locator(hours)
    t_axes.xaxis.set_major_formatter(hours_fmt)

    if obs.is_night(Time(start)):
        plt.axvspan(start, morning_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.5)
        plt.axvspan(morning_astronomical_twilight, morning_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
        plt.axvspan(morning_nautical_twilight, morning_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
        plt.axvspan(morning_civil_twilight, sunrise, ymin=0, ymax=1, color='blue', alpha=0.1)
        plt.axvspan(sunset, evening_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.1)
        plt.axvspan(evening_civil_twilight, evening_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
        plt.axvspan(evening_nautical_twilight, evening_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
        plt.axvspan(evening_astronomical_twilight, end, ymin=0, ymax=1, color='blue', alpha=0.5)
    else:
        plt.axvspan(sunset, evening_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.1)
        plt.axvspan(evening_civil_twilight, evening_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
        plt.axvspan(evening_nautical_twilight, evening_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
        plt.axvspan(evening_astronomical_twilight, morning_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.5)
        plt.axvspan(morning_astronomical_twilight, morning_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
        plt.axvspan(morning_nautical_twilight, morning_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
        plt.axvspan(morning_civil_twilight, sunrise, ymin=0, ymax=1, color='blue', alpha=0.1)

    tlh_axes = plt.axes(plot_positions[0][1])
    plt.title('Last Hour')
    tlh_axes.plot_date(time, amb_temp, 'ko',\
                       markersize=2, markeredgewidth=0,\
                       drawstyle="default")
    plt.grid(which='major', color='k')
    plt.yticks(range(-100,100,10))
    tlh_axes.xaxis.set_major_locator(hours)
    tlh_axes.xaxis.set_major_formatter(hours_fmt)
    tlh_axes.yaxis.set_ticklabels([])
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    plt.ylim(-5,35)


    ##-------------------------------------------------------------------------
    ## Plot Temperature Difference vs. Time
    td_axes = plt.axes(plot_positions[1][0])
    temp_diff = [x['data']['Sky Temperature (C)'] - x['data']['Ambient Temperature (C)']\
                 for x in entries\
                 if 'Sky Temperature (C)' in x['data'].keys()\
                 and 'Ambient Temperature (C)' in x['data'].keys()\
                 and 'Sky Safe' in x['data'].keys()]
    sky_safe = [x['data']['Sky Safe']\
                for x in entries\
                if 'Sky Temperature (C)' in x['data'].keys()\
                and 'Ambient Temperature (C)' in x['data'].keys()\
                and 'Sky Safe' in x['data'].keys()]
    time = [x['date'] for x in entries\
            if 'Sky Temperature (C)' in x['data'].keys()\
            and 'Ambient Temperature (C)' in x['data'].keys()\
            and 'Sky Safe' in x['data'].keys()]
    td_axes.plot_date(time, temp_diff, 'ko-', label='Cloudiness',\
                      markersize=2, markeredgewidth=0,\
                      drawstyle="default")
    td_axes.fill_between(time, -60, temp_diff, where=np.array(sky_safe)==1,\
                         color='green', alpha=0.5)
    td_axes.fill_between(time, -60, temp_diff, where=np.array(sky_safe)==0,\
                         color='red', alpha=0.5)
    plt.ylabel("Cloudiness")
    plt.grid(which='major', color='k')
    plt.yticks(range(-100,100,10))
    plt.xlim(start, end)
    plt.ylim(-60,10)
    td_axes.xaxis.set_major_locator(hours)
    td_axes.xaxis.set_major_formatter(hours_fmt)
    td_axes.xaxis.set_ticklabels([])

    tdlh_axes = plt.axes(plot_positions[1][1])
    tdlh_axes.plot_date(time, temp_diff, 'ko-', label='Cloudiness',\
                        markersize=2, markeredgewidth=0,\
                        drawstyle="default")
    tdlh_axes.fill_between(time, -60, temp_diff, where=np.array(sky_safe)==1,\
                           color='green', alpha=0.5)
    tdlh_axes.fill_between(time, -60, temp_diff, where=np.array(sky_safe)==0,\
                           color='red', alpha=0.5)
    plt.grid(which='major', color='k')
    plt.yticks(range(-100,100,10))
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    plt.ylim(-60,10)
    tdlh_axes.xaxis.set_major_locator(hours)
    tdlh_axes.xaxis.set_major_formatter(hours_fmt)
    tdlh_axes.xaxis.set_ticklabels([])
    tdlh_axes.yaxis.set_ticklabels([])


    ##-------------------------------------------------------------------------
    ## Plot Wind Speed vs. Time
    w_axes = plt.axes(plot_positions[2][0])
    wind_speed = [x['data']['Wind Speed (km/h)']\
                  for x in entries\
                  if 'Wind Speed (km/h)' in x['data'].keys()\
                  and 'Wind Safe' in x['data'].keys()\
                  and 'Gust Safe' in x['data'].keys()]
    wind_safe = [int(x['data']['Wind Safe']) + 2*int(x['data']['Gust Safe'])\
                  for x in entries\
                  if 'Wind Speed (km/h)' in x['data'].keys()\
                  and 'Wind Safe' in x['data'].keys()\
                  and 'Gust Safe' in x['data'].keys()]
    wind_mavg = movingaverage(wind_speed, 10)
    time = [x['date'] for x in entries\
                if 'Wind Speed (km/h)' in x['data'].keys()\
                and 'Wind Safe' in x['data'].keys()\
                and 'Gust Safe' in x['data'].keys()]
    w_axes.plot_date(time, wind_speed, 'ko', alpha=0.5,\
                     markersize=2, markeredgewidth=0,\
                     drawstyle="default")
    w_axes.plot_date(time, wind_mavg, 'b-',\
                     label='Wind Speed',\
                     markersize=3, markeredgewidth=0,\
                     drawstyle="default")
    w_axes.plot_date([start, end], [0, 0], 'k-',ms=1)
    w_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==3,\
                         color='green', alpha=0.5)
    ## Gust Safe, Wind not Safe
    w_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==2,\
                         color='red', alpha=0.4)
    ## Gust not Safe, Wind Safe
    w_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==1,\
                         color='red', alpha=0.6)
    ## Gust not Safe, Wind not Safe
    w_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==0,\
                         color='red', alpha=0.8)
    plt.ylabel("Wind (km/h)")
    plt.grid(which='major', color='k')
    plt.yticks(range(-100,100,10))
    plt.xlim(start, end)
    wind_max = max([45, np.ceil(max(wind_speed)/5.)*5.])
    plt.ylim(0,75)
    w_axes.xaxis.set_major_locator(hours)
    w_axes.xaxis.set_major_formatter(hours_fmt)
    w_axes.xaxis.set_ticklabels([])


    wlh_axes = plt.axes(plot_positions[2][1])
    wlh_axes.plot_date(time, wind_speed, 'ko', alpha=0.5,\
                     markersize=2, markeredgewidth=0,\
                     drawstyle="default")
    wlh_axes.plot_date(time, wind_mavg, 'b-',\
                     label='Wind Speed',\
                     markersize=3, markeredgewidth=0,\
                     drawstyle="default")
    wlh_axes.plot_date([start, end], [0, 0], 'k-',ms=1)
    wlh_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==3,\
                         color='green', alpha=0.5)
    ## Gust Safe, Wind not Safe
    wlh_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==2,\
                         color='red', alpha=0.4)
    ## Gust not Safe, Wind Safe
    wlh_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==1,\
                         color='red', alpha=0.6)
    ## Gust not Safe, Wind not Safe
    wlh_axes.fill_between(time, -5, wind_speed, where=np.array(wind_safe)==0,\
                         color='red', alpha=0.8)
    plt.grid(which='major', color='k')
    plt.yticks(range(-100,100,10))
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    wind_max = max([45, np.ceil(max(wind_speed)/5.)*5.])
    plt.ylim(0,75)
    wlh_axes.xaxis.set_major_locator(hours)
    wlh_axes.xaxis.set_major_formatter(hours_fmt)
    wlh_axes.xaxis.set_ticklabels([])
    wlh_axes.yaxis.set_ticklabels([])


    ##-------------------------------------------------------------------------
    ## Plot Rain Frequency vs. Time
    rf_axes = plt.axes(plot_positions[3][0])
    rf_value = [x['data']['Rain Frequency']\
                  for x in entries\
                  if 'Rain Frequency' in x['data'].keys()\
                  and 'Rain Safe' in x['data'].keys()]
    rain_safe = [int(x['data']['Rain Safe'])\
                 for x in entries\
                 if 'Rain Frequency' in x['data'].keys()\
                 and 'Rain Safe' in x['data'].keys()]
    time = [x['date'] for x in entries\
            if 'Rain Frequency' in x['data'].keys()\
            and 'Rain Safe' in x['data'].keys()]
    rf_axes.plot_date(time, rf_value, 'ko-', label='Rain',\
                      markersize=2, markeredgewidth=0,\
                      drawstyle="default")
    rf_axes.plot_date([start,end], [260,260], 'k-')
    rf_axes.fill_between(time, 0, rf_value, where=np.array(rain_safe)==1,\
                         color='green', alpha=0.5)
    rf_axes.fill_between(time, 0, rf_value, where=np.array(rain_safe)==0,\
                         color='red', alpha=0.5)
    plt.ylabel("Rain Sensor")
    plt.grid(which='major', color='k')
    plt.ylim(120,275)
    plt.xlim(start, end)
    rf_axes.xaxis.set_major_locator(hours)
    rf_axes.xaxis.set_major_formatter(hours_fmt)
    rf_axes.xaxis.set_ticklabels([])
    rf_axes.yaxis.set_ticklabels([])

    rflh_axes = plt.axes(plot_positions[3][1])
    rflh_axes.plot_date(time, rf_value, 'ko-', label='Rain',\
                      markersize=2, markeredgewidth=0,\
                      drawstyle="default")
    rflh_axes.plot_date([start,end], [260,260], 'k-')
    rflh_axes.fill_between(time, 0, rf_value, where=np.array(rain_safe)==1,\
                         color='green', alpha=0.5)
    rflh_axes.fill_between(time, 0, rf_value, where=np.array(rain_safe)==0,\
                         color='red', alpha=0.5)
    plt.grid(which='major', color='k')
    plt.ylim(120,275)
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    rflh_axes.xaxis.set_major_locator(hours)
    rflh_axes.xaxis.set_major_formatter(hours_fmt)
    rflh_axes.xaxis.set_ticklabels([])
    rflh_axes.yaxis.set_ticklabels([])


    ##-------------------------------------------------------------------------
    ## Safe/Unsafe vs. Time
    safe_axes = plt.axes(plot_positions[4][0])
    safe_value = [int(x['data']['Safe'])\
                  for x in entries\
                  if 'Safe' in x['data'].keys()]
    safe_time = [x['date'] for x in entries\
                  if 'Safe' in x['data'].keys()]
    safe_axes.plot_date(safe_time, safe_value, 'ko',\
                       markersize=2, markeredgewidth=0,\
                       drawstyle="default")
    safe_axes.fill_between(safe_time, -1, safe_value, where=np.array(safe_value)==1,\
                     color='green', alpha=0.5)
    safe_axes.fill_between(safe_time, -1, safe_value, where=np.array(safe_value)==0,\
                     color='red', alpha=0.5)
    plt.ylabel("Safe")
    plt.xlim(start, end)
    plt.ylim(-0.1, 1.1)
    plt.yticks([0,1])
    plt.grid(which='major', color='k')
    safe_axes.xaxis.set_major_locator(hours)
    safe_axes.xaxis.set_major_formatter(hours_fmt)
    safe_axes.xaxis.set_ticklabels([])
    safe_axes.yaxis.set_ticklabels([])

    safelh_axes = plt.axes(plot_positions[4][1])
    safelh_axes.plot_date(safe_time, safe_value, 'ko',\
                       markersize=2, markeredgewidth=0,\
                       drawstyle="default")
    safelh_axes.fill_between(safe_time, -1, safe_value, where=np.array(safe_value)==1,\
                     color='green', alpha=0.5)
    safelh_axes.fill_between(safe_time, -1, safe_value, where=np.array(safe_value)==0,\
                     color='red', alpha=0.5)
    plt.ylim(-0.1, 1.1)
    plt.yticks([0,1])
    plt.grid(which='major', color='k')
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    safelh_axes.xaxis.set_major_locator(hours)
    safelh_axes.xaxis.set_major_formatter(hours_fmt)
    safelh_axes.xaxis.set_ticklabels([])
    safelh_axes.yaxis.set_ticklabels([])


    ##-------------------------------------------------------------------------
    ## Plot PWM Value vs. Time
    pwm_axes = plt.axes(plot_positions[5][0])
    plt.ylabel("Heater (%)")
    plt.ylim(-5,105)
    plt.xlim(start, end)
    plt.grid(which='major', color='k')
    rst_axes = pwm_axes.twinx()
    plt.ylim(0,25)
    plt.xlim(start, end)
    pwm_value = [x['data']['PWM Value']\
                  for x in entries\
                  if 'PWM Value' in x['data'].keys()\
                  and 'Rain Sensor Temp (C)' in x['data'].keys()\
                  and 'Ambient Temperature (C)' in x['data'].keys()]
    rst_delta = [x['data']['Rain Sensor Temp (C)'] - x['data']['Ambient Temperature (C)']\
                 for x in entries\
                 if 'PWM Value' in x['data'].keys()\
                 and 'Rain Sensor Temp (C)' in x['data'].keys()\
                 and 'Ambient Temperature (C)' in x['data'].keys()]
    time = [x['date'] for x in entries\
            if 'PWM Value' in x['data'].keys()\
            and 'Rain Sensor Temp (C)' in x['data'].keys()\
            and 'Ambient Temperature (C)' in x['data'].keys()]
    rst_axes.set_ylabel('Sensor DeltaT (C)')
    rst_axes.plot_date(time, rst_delta, 'ro-', alpha=0.5,\
                       label='RST Delta (C)',\
                       markersize=2, markeredgewidth=0,\
                       drawstyle="default")
    pwm_axes.plot_date(time, pwm_value, 'bo', label='Heater',\
                       markersize=2, markeredgewidth=0,\
                       drawstyle="default")
    pwm_axes.xaxis.set_major_locator(hours)
    pwm_axes.xaxis.set_major_formatter(hours_fmt)


    pwmlh_axes = plt.axes(plot_positions[5][1])
    plt.ylim(-5,105)
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    plt.grid(which='major', color='k')
    rstlh_axes = pwmlh_axes.twinx()
    plt.ylim(0,25)
    plt.xlim(date-tdelta(0, 60*60), date+tdelta(0, 5*60))
    rstlh_axes.plot_date(time, rst_delta, 'ro-', alpha=0.5,\
                         label='RST Delta (C)',\
                         markersize=2, markeredgewidth=0,\
                         drawstyle="default")
    rstlh_axes.xaxis.set_ticklabels([])
    rstlh_axes.yaxis.set_ticklabels([])
    pwmlh_axes.plot_date(time, pwm_value, 'bo', label='Heater',\
                       markersize=2, markeredgewidth=0,\
                       drawstyle="default")
    pwmlh_axes.xaxis.set_major_locator(hours)
    pwmlh_axes.xaxis.set_major_formatter(hours_fmt)
    pwmlh_axes.yaxis.set_ticklabels([])



    ##-------------------------------------------------------------------------
    ## Plot Brightness vs. Time
#     ldr_axes = plt.axes(plot_positions[3][0])
#     max_ldr = 28587999.99999969
#     ldr_value = [x['data']['LDR Resistance (ohm)']\
#                   for x in entries\
#                   if 'LDR Resistance (ohm)' in x['data'].keys()]
#     brightness = [10.**(2. - 2.*x/max_ldr) for x in ldr_value]
#     time = [x['date'] for x in entries\
#                 if 'LDR Resistance (ohm)' in x['data'].keys()]
#     ldr_axes.plot_date(time, brightness, 'ko',\
#                        markersize=2, markeredgewidth=0,\
#                        drawstyle="default")
#     plt.ylabel("Brightness (%)")
#     plt.yticks(range(-100,100,10))
#     plt.ylim(-5,105)
#     plt.grid(which='major', color='k')
#     ldr_axes.xaxis.set_major_locator(hours)
#     ldr_axes.xaxis.set_major_formatter(hours_fmt)
#     plt.xlim(start, end)
# 
#     if obs.is_night(start):
#         plt.axvspan(start, morning_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.5)
#         plt.axvspan(morning_astronomical_twilight, morning_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
#         plt.axvspan(morning_nautical_twilight, morning_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
#         plt.axvspan(morning_civil_twilight, sunrise, ymin=0, ymax=1, color='blue', alpha=0.1)
#         plt.axvspan(sunset, evening_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.1)
#         plt.axvspan(evening_civil_twilight, evening_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
#         plt.axvspan(evening_nautical_twilight, evening_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
#         plt.axvspan(evening_astronomical_twilight, end, ymin=0, ymax=1, color='blue', alpha=0.5)
#     else:
#         plt.axvspan(sunset, evening_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.1)
#         plt.axvspan(evening_civil_twilight, evening_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
#         plt.axvspan(evening_nautical_twilight, evening_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
#         plt.axvspan(evening_astronomical_twilight, morning_astronomical_twilight, ymin=0, ymax=1, color='blue', alpha=0.5)
#         plt.axvspan(morning_astronomical_twilight, morning_nautical_twilight, ymin=0, ymax=1, color='blue', alpha=0.3)
#         plt.axvspan(morning_nautical_twilight, morning_civil_twilight, ymin=0, ymax=1, color='blue', alpha=0.2)
#         plt.axvspan(morning_civil_twilight, sunrise, ymin=0, ymax=1, color='blue', alpha=0.1)


    ##-------------------------------------------------------------------------
    plot_filename = '{}.png'.format(date_string)
    plot_file = os.path.expanduser('~panoptes/weather_plots/{}'.format(plot_filename))
    plt.savefig(plot_file, dpi=dpi, bbox_inches='tight', pad_inches=0.10)


if __name__ == '__main__':
    ##-------------------------------------------------------------------------
    ## Parse Command Line Arguments
    ##-------------------------------------------------------------------------
    ## create a parser object for understanding command-line arguments
    parser = argparse.ArgumentParser(
             description="Program description.")
    ## add flags
    parser.add_argument("-v", "--verbose",
        action="store_true", dest="verbose",
        default=False, help="Be verbose.")
    parser.add_argument("-p", "--plot",
        action="store_true", dest="plot",
        default=False, help="Plot the data instead of querying new values.")
    parser.add_argument("-1", "--one",
        action="store_true", dest="one",
        default=False, help="Make one query only (default is infinite loop).")
    parser.add_argument("--no_mongo",
        action="store_false", dest="mongo",
        default=True, help="Do not send results to mongo database.")
    ## add arguments for telemetry queries
    parser.add_argument("--device",
        type=str, dest="device",
        help="Device address for the weather station (default = /dev/ttyUSB0)")
    parser.add_argument("-i", "--interval",
        type=float, dest="interval",
        default=10.,
        help="Time (in seconds) to wait between queries (default = 30 s)")
    ## add arguments for plot
    parser.add_argument("-d", "--date",
        type=str, dest="date",
        default=None,
        help="UT Date to plot")

    args = parser.parse_args()


    if not args.plot:
        ##-------------------------------------------------------------------------
        ## Update Weather Telemetry
        ##-------------------------------------------------------------------------
        AAG = AAGCloudSensor(serial_address=args.device)
        if args.one:
            AAG.update_weather(update_mongo=args.mongo)
        else:
            now = dt.utcnow()
            while True:
                last = now
                now = dt.utcnow()
                loop_duration = (now - last).total_seconds()/60.
                AAG.update_weather(update_mongo=args.mongo)
                AAG.calculate_and_set_PWM()
                AAG.logger.info('Sleeping for {:.0f} seconds ...'.format(args.interval))
                AAG.logger.info('')
                time.sleep(args.interval)
    else:
        plot_weather(args.date)
