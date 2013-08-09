#!/usr/bin/python

#Tests sending a request data page to a device

import sys
import logging
import time

import antd.ant as ant
import antd.hw  as hw

from antd.ant   import AntTimeoutError

from antd.ant   import RequestDataPage, DATA_PAGE_COMMON_MANUFACTURERS_INFO, DATA_PAGE_REQUEST_DATA
from antd.ant   import HeartRatePage0, HeartRatePage1, HeartRatePage2, HeartRatePage3, HeartRatePage4
from antd.antfs import Beacon

logging.basicConfig(
        level=logging.DEBUG,
        out=sys.stderr,
        format="[%(threadName)s]\t%(asctime)s\t%(levelname)s\t%(message)s")
_LOG = logging.getLogger()

#Tested with Garmin Forerunner 410 (acks request but does not send)
#Tested with Garmin Soft HRM (ignores request)
def listen_for_broadcast_then_request_data_page(session, isGPSNotHRM):
        network = session.networks[0]
        if isGPSNotHRM:
            network.set_key("\xa8\xa4\x23\xb9\xf5\x5e\x63\xc1")
        else:
            network.set_key('\xB9\xA5\x21\xFB\xBD\x72\xC3\x45')
        channel = session.channels[0]
        channel.assign(channel_type=0x00, network_number=0)
        if isGPSNotHRM:
            channel.set_id(device_number=0, device_type_id=0, trans_type=0)
            channel.set_period(0x4000)
            channel.set_rf_freq(50)
        else:
            channel.set_id(device_number=0, device_type_id=120, trans_type=0)
            channel.set_period(8070)
            channel.set_rf_freq(57)
            
        channel.set_search_waveform(0x0053)
        channel.set_search_timeout(20)
        channel.open()
        
        _LOG.info("Wait on a broadcast. See if there is something there and set period to matching if we can....")
        broadcast_data = channel.recv_broadcast( timeout=5 )

        beacon = Beacon.unpack(broadcast_data)
        if beacon:
            print "ANT-FS | Beacon : " + str(beacon)
            #Matching the period. Gives cleaner logs.
            period_hz = 2 ** (beacon.period - 1)
            channel_period = 0x8000 / period_hz
            channel.set_period( channel_period )
            
        heart_rate = HeartRatePage0.unpack(broadcast_data)
        if not(heart_rate):
            heart_rate = HeartRatePage1.unpack(broadcast_data)
        if not(heart_rate):
            heart_rate = HeartRatePage2.unpack(broadcast_data)
        if not(heart_rate):
            heart_rate = HeartRatePage3.unpack(broadcast_data)
        if not(heart_rate):
            heart_rate = HeartRatePage4.unpack(broadcast_data)
        if heart_rate:
            if(heart_rate):
                heart_rate_comp = str(heart_rate.computed_heart_rate)
            else:
                heart_rate_comp = "INVALID"
            print "HRM | Heart Rate of " + heart_rate_comp + " BPM : " + str( heart_rate )

        time.sleep(.750)

                
        print "------- " + "Send Common Data Page Request for Page "+str(DATA_PAGE_COMMON_MANUFACTURERS_INFO)+" and wait for Ack" + " -----------"
        from antd.ant import AntTxFailedError
        try:
            req_data_page = RequestDataPage( data_page_number=DATA_PAGE_REQUEST_DATA, reserved1=0xFF, reserved2=0xFF, descriptor_byte1=0x00, descriptor_byte2=0x00, requested_transmission_response=0x80, requested_page_number=DATA_PAGE_COMMON_MANUFACTURERS_INFO, command_id=0x01 )
            channel.send_acknowledged( req_data_page.pack() )
        except AntTxFailedError, e:
            print "---------------- " + "Failure to get an Ack" + " -----------------------"
        else:
            print "---------------- " + "Received Ack" + " -----------------------"

        #I never received a data page response. I was just watching the log here.

        time.sleep(5)
        print "---------------- " + "End of wait" + " -----------------------"
########################



def main():

    dev = hw.UsbHardware()
    core = ant.Core(dev)

    print ">>>>>>>>>>>>>>>>  Garmin (Soft) HRM >>>>>>>>>>>>>>>>>>>>>>>>>>>>>"

    session = ant.Session(core)
    try:
        listen_for_broadcast_then_request_data_page(session, isGPSNotHRM=False)
    except AntTimeoutError:
        _LOG.warning("Timed out - Device not present?.")
    finally:
        try: session.close()
        except: _LOG.warning("Caught exception while resetting system.", exc_info=True)

    print ">>>>>>>>>>>>>>>>>  Garmin Forerunner 410 >>>>>>>>>>>>>>>>>>>>>>>"

    session = ant.Session(core)
    try:
        listen_for_broadcast_then_request_data_page(session, isGPSNotHRM=True)
    except AntTimeoutError:
        _LOG.warning("Timed out - Device not present?.")
    finally:
        try: session.close()
        except: _LOG.warning("Caught exception while resetting system.", exc_info=True)

    print "---------------- " + "Finishing" + " -----------------------"
########################


if __name__ == "__main__":
    print "Script = " + sys.argv[0]
    main()
