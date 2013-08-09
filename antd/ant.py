# Copyright (c) 2012, Braiden Kindt.
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 
#   1. Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
# 
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDER AND CONTRIBUTORS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY
# WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


import threading
import logging
import array
import errno
import time
import struct
import collections

_log = logging.getLogger("antd.ant")
_trace = logging.getLogger("antd.trace")

# First byte of a packet
SYNC = 0xA4
# Direction of command
DIR_IN = "IN"
DIR_OUT = "OUT"
# Channel response codes
RESPONSE_NO_ERROR = 0
CHANNEL_IN_WRONG_STATE = 21
CHANNEL_NOT_OPENED = 22
CHANNEL_ID_NOT_SET = 24
CLOSE_ALL_CHANNELS = 25
TRANSFER_IN_PROGRESS = 31
TRANSFER_SEQUENCE_NUMBER_ERROR = 32
TANNSFER_IN_ERROR = 33
MESSAGE_SIZE_EXCEEDS_LIMIT = 39
INVALID_MESSAGE = 40
INVALID_NETWORK_NUMBER = 41
INVALID_LIST_ID = 48
INVALID_SCAN_TX_CHANNEL = 49
INVALID_PARAMETER_PROVIDED = 51
NVM_FULL_ERROR = 64
NVM_WRITE_ERROR = 65
USB_STRING_WRITE_FAIL = 112
MESG_SERIAL_ERROR_ID = 174

# rf event codes
EVENT_RX_SEARCH_TIMEOUT = 1
EVENT_RX_FAIL = 2
EVENT_TX = 3
EVENT_TRANSFER_RX_FAILED = 4
EVENT_TRANSFER_TX_COMPLETED = 5
EVENT_TRANSFER_TX_FAILED = 6
EVENT_CHANNEL_CLOSED = 7
EVENT_RX_FAIL_GO_TO_SEARCH = 8
EVENT_CHANNEL_COLLISION = 9
EVENT_TRANSFER_TX_START = 10
EVENT_SERIAL_QUE_OVERFLOW = 52
EVENT_QUEUE_OVERFLOW = 53

# channel status
CHANNEL_STATUS_UNASSIGNED = 0
CHANNEL_STATUS_ASSIGNED = 1
CHANNEL_STATUS_SEARCHING = 2
CHANNEL_STATUS_TRACKING = 3




class AntError(Exception):
    """
    Default error, unless a more specific error
    instance is provided, usually indicates that
    the ANT hardware rejected command. Usually do
    to invalid state / API usage.
    """
class AntTimeoutError(AntError):
    """
    An expected reply was not received from hardware.
    For "recv_*()" and "read()" operations timeout is
    safely retryable. For other types of commands, do
    not assume timeout means command did not take effect.
    This is particularly true for Acknowledged writes.
    For such timeouts, restarting ANT session is usually
    only course of action.
    """
class AntTxFailedError(AntError):
    """
    An Acknowledged message of burst transfer failed
    to transmit successfully. Retry is typically safe
    but recovery for burst is application dependent.
    """
class AntChannelClosedError(AntError):
    """
    Raise while attempty to read / write to a closed
    channel, or if a channel transitions to closed
    while a read / write is running. (channel may
    be closed due to search timeout expiring.)
    """

class AntDataPageNumberError(AntError):
    """
    DataPageNumber is not appropriate for class being instantiated
    """

def msg_to_string(msg):
    """
    Returns a string representation of
    the provided array (for debug output)
    """
    return array.array("B", msg).tostring().encode("hex")

def is_timeout(ioerror):
    """
    True if ioerror can be categorized as a timeout.
    """
    try:
        # all IOerrors returned by pyusb contain
        # msg, errno, and should be unpackable.
        err, msg = ioerror
    except ValueError:
        # but, sometimes we can't unpack. I don't
        # know what is raising theses IOErrors
        # just assume its a timeout, so operation
        # is retried
        return True
    else:
        return (err == errno.ETIMEDOUT #libusb10
                or msg == "Connection timed out") #libusb01

def generate_checksum(msg):
    """
    Generate a checksum of the given msg.
    xor of all bytes.
    """
    return reduce(lambda x, y: x ^ y, msg)

def validate_checksum(msg):
    """
    Return true if message has valid checksum
    """
    return generate_checksum(msg) == 0

def tokenize_message(msg):
    """
    A generator returning on ant messages
    from the provided string of one or more
    concatenated messages.
    """
    while msg:
        assert msg[0] & 0xFE == SYNC
        length = msg[1]
        yield msg[:4 + length]
        msg = msg[4 + length:]

def data_tostring(data):
    """
    Return a string repenting bytes of given
    data. used by send() methods to convert
    arugment to required string.
    """
    if isinstance(data, list):
        return array.array("B", data).tostring()
    elif isinstance(data, array.array):
        return data.tostring()
    else:
        return data

# retry policies define the strategy used to
# determine if a command should be retried based
# on provided error. They can be configured
# for each ANT message defined below. Retry
# on timeout should be considered dangerous.
# e.g. retrying a timedout acknowledged message
# will certainly fail.

def timeout_retry_policy(error):
    return default_retry_policy(error) or isinstance(error, AntTimeoutError)

def default_retry_policy(error):
    return isinstance(error, AntTxFailedError)

def always_retry_policy(error):
    return True

def never_retry_policy(error):
    return False

def wait_and_retry_policy(error):
    if default_retry_policy(error):
        time.sleep(1)
        return True
    else:
        return False

# matcher define the strategy to determine
# if an incoming message from ANT device should
# update the status of a running command.

def same_channel_or_network_matcher(request, reply):
    return (
            (not hasattr(reply, "channel_number")
             or (hasattr(request, "channel_number") and (0x1f & request.channel_number) == (0x1f & reply.channel_number)))
        or 
            (not hasattr(reply, "network_number")
             or (hasattr(request, "network_number") and request.network_number == reply.network_number)))

def default_matcher(request, reply):
    return (same_channel_or_network_matcher(request, reply) 
            and isinstance(reply, ChannelEvent)
            and reply.msg_id == request.ID)

def reset_matcher(request, reply):
    return isinstance(reply, StartupMessage)

def close_channel_matcher(request, reply):
    return same_channel_or_network_matcher(request, reply) and (
            (isinstance(reply, ChannelEvent)
             and reply.msg_id == CloseChannel.ID
             and reply.msg_code != 0)
        or (isinstance(reply, ChannelEvent)
             and reply.msg_id == 1
             and reply.msg_code == EVENT_CHANNEL_CLOSED))

def request_message_matcher(request, reply):
    return default_matcher(request, reply) or reply.ID == request.msg_id

def recv_broadcast_matcher(request, reply):
    return (close_channel_matcher(request, reply)
        or isinstance(reply, RecvBroadcastData))

def send_data_matcher(request, reply):
    return (close_channel_matcher(request, reply)
        or (isinstance(reply, ChannelEvent)
            and reply.msg_id == 1
            and reply.msg_code in (EVENT_TX, EVENT_TRANSFER_TX_COMPLETED, EVENT_TRANSFER_TX_FAILED)))

# validators define strategy for determining
# if a give reply from ANT should raise an
# error. 

def default_validator(request, reply):
    if isinstance(reply, ChannelEvent) and reply.msg_code in (EVENT_CHANNEL_CLOSED, CHANNEL_NOT_OPENED):
        return AntChannelClosedError("Channel closed. %s" % reply)
    elif isinstance(reply, ChannelEvent) and reply.msg_code != RESPONSE_NO_ERROR:
        return AntError("Failed to execute command message_code=%d. %s" % (reply.msg_code, reply))

def close_channel_validator(request, reply):
    if not (isinstance(reply, ChannelEvent) and reply.msg_id == 1 and reply.msg_code == EVENT_CHANNEL_CLOSED):
        return default_validator(request, reply)

def send_data_validator(request, reply):
    if isinstance(reply, ChannelEvent) and reply.msg_id == 1 and reply.msg_code == EVENT_TRANSFER_TX_FAILED:
        return AntTxFailedError("Send message was not acknowledged by peer. %s" % reply)
    elif not (isinstance(reply, ChannelEvent) and reply.msg_id == 1 and reply.msg_code in (EVENT_TX, EVENT_TRANSFER_TX_COMPLETED)):
        return default_validator(request, reply)

#String from message code

#See more [better definitions] @ https://code.google.com/p/msp430ant/source/browse/tools/pc_serial_c/antdefines.h
#Plus there are some in the docs that aren't here....
#TODO - This will be cleaner/single point of change if we make this come from an enum directly
def message_code_lookup(code):
# channel response codes
    if (code == RESPONSE_NO_ERROR):
        return "RESPONSE_NO_ERROR"
    elif (code == CHANNEL_IN_WRONG_STATE):
        return "CHANNEL_IN_WRONG_STATE"
    elif (code == CHANNEL_NOT_OPENED):
        return "CHANNEL_NOT_OPENED"
    elif (code == CHANNEL_ID_NOT_SET):
        return "CHANNEL_ID_NOT_SET"
    elif (code == CLOSE_ALL_CHANNELS):
        return "CLOSE_ALL_CHANNELS"
    elif (code == TRANSFER_IN_PROGRESS):
        return "TRANSFER_IN_PROGRESS"
    elif (code == TRANSFER_SEQUENCE_NUMBER_ERROR):
        return "TRANSFER_SEQUENCE_NUMBER_ERROR"
    elif (code == TANNSFER_IN_ERROR):
        return "TANNSFER_IN_ERROR"
    elif (code == MESSAGE_SIZE_EXCEEDS_LIMIT):
        return "MESSAGE_SIZE_EXCEEDS_LIMIT"
    elif (code == INVALID_MESSAGE):
        return "INVALID_MESSAGE"
    elif (code == INVALID_NETWORK_NUMBER):
        return "INVALID_NETWORK_NUMBER"
    elif (code == INVALID_LIST_ID):
        return "INVALID_LIST_ID"
    elif (code == INVALID_SCAN_TX_CHANNEL):
        return "INVALID_SCAN_TX_CHANNEL"
    elif (code == INVALID_PARAMETER_PROVIDED):
        return "INVALID_PARAMETER_PROVIDED"
    elif (code == NVM_FULL_ERROR):
        return "NVM_FULL_ERROR"
    elif (code == NVM_WRITE_ERROR):
        return "NVM_WRITE_ERROR"
    elif (code == USB_STRING_WRITE_FAIL):
        return "USB_STRING_WRITE_FAIL"
    elif (code == MESG_SERIAL_ERROR_ID):
        return "MESG_SERIAL_ERROR_ID"
    
    elif (code == EVENT_RX_SEARCH_TIMEOUT):
        return "EVENT_RX_SEARCH_TIMEOUT"
    elif (code == EVENT_RX_FAIL):
        return "EVENT_RX_FAIL"
    elif (code == EVENT_TX):
        return "EVENT_TX"
    elif (code == EVENT_TRANSFER_RX_FAILED):
        return "EVENT_TRANSFER_RX_FAILED"
    elif (code == EVENT_TRANSFER_TX_COMPLETED):
        return "EVENT_TRANSFER_TX_COMPLETED"
    elif (code == EVENT_TRANSFER_TX_FAILED):
        return "EVENT_TRANSFER_TX_FAILED"
    elif (code == EVENT_CHANNEL_CLOSED):
        return "EVENT_CHANNEL_CLOSED"
    elif (code == EVENT_RX_FAIL_GO_TO_SEARCH):
        return "EVENT_RX_FAIL_GO_TO_SEARCH"
    elif (code == EVENT_CHANNEL_COLLISION):
        return "EVENT_CHANNEL_COLLISION"
    elif (code == EVENT_TRANSFER_TX_START):
        return "EVENT_TRANSFER_TX_START"
    elif (code == EVENT_SERIAL_QUE_OVERFLOW):
        return "EVENT_SERIAL_QUE_OVERFLOW"
    elif (code == EVENT_QUEUE_OVERFLOW):
        return "EVENT_QUEUE_OVERFLOW"
    else:
        return "..."

MESG_INVALID_ID                   = 0x00 
MESG_EVENT_ID                     = 0x01
MESG_VERSION_ID                   = 0x3E
MESG_RESPONSE_EVENT_ID            = 0x40
MESG_UNASSIGN_CHANNEL_ID          = 0x41
MESG_ASSIGN_CHANNEL_ID            = 0x42
MESG_CHANNEL_MESG_PERIOD_ID       = 0x43
MESG_CHANNEL_SEARCH_TIMEOUT_ID    = 0x44
MESG_CHANNEL_RADIO_FREQ_ID        = 0x45
MESG_NETWORK_KEY_ID               = 0x46
MESG_RADIO_TX_POWER_ID            = 0x47
MESG_RADIO_CW_MODE_ID             = 0x48
MESG_SEARCH_WAVEFORM_ID           = 0x49
MESG_SYSTEM_RESET_ID              = 0x4A
MESG_OPEN_CHANNEL_ID              = 0x4B
MESG_CLOSE_CHANNEL_ID             = 0x4C
MESG_REQUEST_ID                   = 0x4D
MESG_BROADCAST_DATA_ID            = 0x4E
MESG_ACKNOWLEDGED_DATA_ID         = 0x4F
MESG_BURST_DATA_ID                = 0x50
MESG_CHANNEL_ID_ID                = 0x51
MESG_CHANNEL_STATUS_ID            = 0x52
MESG_RADIO_CW_INIT_ID             = 0x53
MESG_CAPABILITIES_ID              = 0x54
MESG_NVM_DATA_ID                  = 0x56
MESG_NVM_CMD_ID                   = 0x57
MESG_NVM_STRING_ID                = 0x58
MESG_ID_LIST_ADD_ID               = 0x59
MESG_ID_LIST_CONFIG_ID            = 0x5A
MESG_OPEN_RX_SCAN_ID              = 0x5B
MESG_EXT_CHANNEL_RADIO_FREQ_ID    = 0x5C
MESG_EXT_BROADCAST_DATA_ID        = 0x5D
MESG_EXT_ACKNOWLEDGED_DATA_ID     = 0x5E
MESG_EXT_BURST_DATA_ID            = 0x5F
MESG_CHANNEL_RADIO_TX_POWER_ID    = 0x60
MESG_GET_SERIAL_NUM_ID            = 0x61
MESG_GET_TEMP_CAL_ID              = 0x62
MESG_SET_LP_SEARCH_TIMEOUT_ID     = 0x63
MESG_SET_TX_SEARCH_ON_NEXT_ID     = 0x64
MESG_SERIAL_NUM_SET_CHANNEL_ID_ID = 0x65
MESG_RX_EXT_MESGS_ENABLE_ID       = 0x66
MESG_RADIO_CONFIG_ALWAYS_ID       = 0x67
MESG_ENABLE_LED_FLASH_ID          = 0x68
MESG_AGC_CONFIG_ID                = 0x6A
MESG_READ_SEGA_ID                 = 0xA0
MESG_SEGA_CMD_ID                  = 0xA1
MESG_SEGA_DATA_ID                 = 0xA2
MESG_SEGA_ERASE_ID                = 0XA3  
MESG_SEGA_WRITE_ID                = 0XA4
MESG_SEGA_LOCK_ID                 = 0xA6
MESG_FUSECHECK_ID                 = 0xA7
MESG_UARTREG_ID                   = 0XA8
MESG_MAN_TEMP_ID                  = 0xA9
MESG_BIST_ID                      = 0XAA
MESG_SELFERASE_ID                 = 0XAB
MESG_SET_MFG_BITS_ID              = 0xAC
MESG_UNLOCK_INTERFACE_ID          = 0xAD
MESG_IO_STATE_ID                  = 0xB0
MESG_CFG_STATE_ID                 = 0xB1
MESG_RSSI_ID                      = 0xC0
MESG_RSSI_BROADCAST_DATA_ID       = 0xC1
MESG_RSSI_ACKNOWLEDGED_DATA_ID    = 0xC2
MESG_RSSI_BURST_DATA_ID           = 0xC3
MESG_RSSI_SEARCH_THRESHOLD_ID     = 0xC4
MESG_BTH_BROADCAST_DATA_ID        = 0xD0
MESG_BTH_ACKNOWLEDGED_DATA_ID     = 0xD1
MESG_BTH_BURST_DATA_ID            = 0xD2
MESG_BTH_EXT_BROADCAST_DATA_ID    = 0xD3
MESG_BTH_EXT_ACKNOWLEDGED_DATA_ID = 0xD4
MESG_BTH_EXT_BURST_DATA_ID        = 0xD5


#https://code.google.com/p/msp430ant/source/browse/tools/pc_serial_c/antmessage.h
#TODO - This will be cleaner/single point of change if we make this come from an enum directly
def message_id_lookup(id):
    if (id == MESG_INVALID_ID):
        return "MESG_INVALID_ID"
    elif (id == MESG_EVENT_ID):
        return "MESG_EVENT_ID"
    elif (id == MESG_VERSION_ID):
        return "MESG_VERSION_ID"
    elif (id == MESG_RESPONSE_EVENT_ID):
        return "MESG_RESPONSE_EVENT_ID"
    elif (id == MESG_UNASSIGN_CHANNEL_ID):
        return "MESG_UNASSIGN_CHANNEL_ID"
    elif (id == MESG_ASSIGN_CHANNEL_ID):
        return "MESG_ASSIGN_CHANNEL_ID"
    elif (id == MESG_CHANNEL_MESG_PERIOD_ID):
        return "MESG_CHANNEL_MESG_PERIOD_ID"
    elif (id == MESG_CHANNEL_SEARCH_TIMEOUT_ID):
        return "MESG_CHANNEL_SEARCH_TIMEOUT_ID"
    elif (id == MESG_CHANNEL_RADIO_FREQ_ID):
        return "MESG_CHANNEL_RADIO_FREQ_ID"
    elif (id == MESG_NETWORK_KEY_ID):
        return "MESG_NETWORK_KEY_ID"
    elif (id == MESG_RADIO_TX_POWER_ID):
        return "MESG_RADIO_TX_POWER_ID"
    elif (id == MESG_RADIO_CW_MODE_ID):
        return "MESG_RADIO_CW_MODE_ID"
    elif (id == MESG_SEARCH_WAVEFORM_ID):
        return "MESG_SEARCH_WAVEFORM_ID"
    elif (id == MESG_SYSTEM_RESET_ID):
        return "MESG_SYSTEM_RESET_ID"
    elif (id == MESG_OPEN_CHANNEL_ID):
        return "MESG_OPEN_CHANNEL_ID"
    elif (id == MESG_CLOSE_CHANNEL_ID):
        return "MESG_CLOSE_CHANNEL_ID"
    elif (id == MESG_REQUEST_ID):
        return "MESG_REQUEST_ID"
    elif (id == MESG_BROADCAST_DATA_ID):
        return "MESG_BROADCAST_DATA_ID"
    elif (id == MESG_ACKNOWLEDGED_DATA_ID):
        return "MESG_ACKNOWLEDGED_DATA_ID"
    elif (id == MESG_BURST_DATA_ID):
        return "MESG_BURST_DATA_ID"
    elif (id == MESG_CHANNEL_ID_ID):
        return "MESG_CHANNEL_ID_ID"
    elif (id == MESG_CHANNEL_STATUS_ID):
        return "MESG_CHANNEL_STATUS_ID"
    elif (id == MESG_RADIO_CW_INIT_ID):
        return "MESG_RADIO_CW_INIT_ID"
    elif (id == MESG_CAPABILITIES_ID):
        return "MESG_CAPABILITIES_ID"
    elif (id == MESG_NVM_DATA_ID):
        return "MESG_NVM_DATA_ID"
    elif (id == MESG_NVM_CMD_ID):
        return "MESG_NVM_CMD_ID"
    elif (id == MESG_NVM_STRING_ID):
        return "MESG_NVM_STRING_ID"
    elif (id == MESG_ID_LIST_ADD_ID):
        return "MESG_ID_LIST_ADD_ID"
    elif (id == MESG_ID_LIST_CONFIG_ID):
        return "MESG_ID_LIST_CONFIG_ID"
    elif (id == MESG_OPEN_RX_SCAN_ID):
        return "MESG_OPEN_RX_SCAN_ID"
    elif (id == MESG_EXT_CHANNEL_RADIO_FREQ_ID):
        return "MESG_EXT_CHANNEL_RADIO_FREQ_ID"
    elif (id == MESG_EXT_BROADCAST_DATA_ID):
        return "MESG_EXT_BROADCAST_DATA_ID"
    elif (id == MESG_EXT_ACKNOWLEDGED_DATA_ID):
        return "MESG_EXT_ACKNOWLEDGED_DATA_ID"
    elif (id == MESG_EXT_BURST_DATA_ID):
        return "MESG_EXT_BURST_DATA_ID"
    elif (id == MESG_CHANNEL_RADIO_TX_POWER_ID):
        return "MESG_CHANNEL_RADIO_TX_POWER_ID"
    elif (id == MESG_GET_SERIAL_NUM_ID):
        return "MESG_GET_SERIAL_NUM_ID"
    elif (id == MESG_GET_TEMP_CAL_ID):
        return "MESG_GET_TEMP_CAL_ID"
    elif (id == MESG_SET_LP_SEARCH_TIMEOUT_ID):
        return "MESG_SET_LP_SEARCH_TIMEOUT_ID"
    elif (id == MESG_SET_TX_SEARCH_ON_NEXT_ID):
        return "MESG_SET_TX_SEARCH_ON_NEXT_ID"
    elif (id == MESG_SERIAL_NUM_SET_CHANNEL_ID_ID):
        return "MESG_SERIAL_NUM_SET_CHANNEL_ID_ID"
    elif (id == MESG_RX_EXT_MESGS_ENABLE_ID):
        return "MESG_RX_EXT_MESGS_ENABLE_ID"
    elif (id == MESG_RADIO_CONFIG_ALWAYS_ID):
        return "MESG_RADIO_CONFIG_ALWAYS_ID"
    elif (id == MESG_ENABLE_LED_FLASH_ID):
        return "MESG_ENABLE_LED_FLASH_ID"
    elif (id == MESG_AGC_CONFIG_ID):
        return "MESG_AGC_CONFIG_ID"
    elif (id == MESG_READ_SEGA_ID):
        return "MESG_READ_SEGA_ID"
    elif (id == MESG_SEGA_CMD_ID):
        return "MESG_SEGA_CMD_ID"
    elif (id == MESG_SEGA_DATA_ID):
        return "MESG_SEGA_DATA_ID"
    elif (id == MESG_SEGA_ERASE_ID):
        return "MESG_SEGA_ERASE_ID"  
    elif (id == MESG_SEGA_WRITE_ID):
        return "MESG_SEGA_WRITE_ID"
    elif (id == MESG_SEGA_LOCK_ID):
        return "MESG_SEGA_LOCK_ID"
    elif (id == MESG_FUSECHECK_ID):
        return "MESG_FUSECHECK_ID"
    elif (id == MESG_UARTREG_ID):
        return "MESG_UARTREG_ID"
    elif (id == MESG_MAN_TEMP_ID):
        return "MESG_MAN_TEMP_ID"
    elif (id == MESG_BIST_ID):
        return "MESG_BIST_ID"
    elif (id == MESG_SELFERASE_ID):
        return "MESG_SELFERASE_ID"
    elif (id == MESG_SET_MFG_BITS_ID):
        return "MESG_SET_MFG_BITS_ID"
    elif (id == MESG_UNLOCK_INTERFACE_ID):
        return "MESG_UNLOCK_INTERFACE_ID"
    elif (id == MESG_IO_STATE_ID):
        return "MESG_IO_STATE_ID"
    elif (id == MESG_CFG_STATE_ID):
        return "MESG_CFG_STATE_ID"
    elif (id == MESG_RSSI_ID):
        return "MESG_RSSI_ID"
    elif (id == MESG_RSSI_BROADCAST_DATA_ID):
        return "MESG_RSSI_BROADCAST_DATA_ID"
    elif (id == MESG_RSSI_ACKNOWLEDGED_DATA_ID):
        return "MESG_RSSI_ACKNOWLEDGED_DATA_ID"
    elif (id == MESG_RSSI_BURST_DATA_ID):
        return "MESG_RSSI_BURST_DATA_ID"
    elif (id == MESG_RSSI_SEARCH_THRESHOLD_ID):
        return "MESG_RSSI_SEARCH_THRESHOLD_ID"
    elif (id == MESG_BTH_BROADCAST_DATA_ID):
        return "MESG_BTH_BROADCAST_DATA_ID"
    elif (id == MESG_BTH_ACKNOWLEDGED_DATA_ID):
        return "MESG_BTH_ACKNOWLEDGED_DATA_ID"
    elif (id == MESG_BTH_BURST_DATA_ID):
        return "MESG_BTH_BURST_DATA_ID"
    elif (id == MESG_BTH_EXT_BROADCAST_DATA_ID):
        return "MESG_BTH_EXT_BROADCAST_DATA_ID"
    elif (id == MESG_BTH_EXT_ACKNOWLEDGED_DATA_ID):
        return "MESG_BTH_EXT_ACKNOWLEDGED_DATA_ID"
    elif (id == MESG_BTH_EXT_BURST_DATA_ID):
        return "MESG_BTH_EXT_BURST_DATA_ID"
    else:
        return "..."


# channel status
def channel_status_lookup(channel_status):
    if (channel_status == CHANNEL_STATUS_UNASSIGNED):
        return "UNASSIGNED"
    elif (channel_status == CHANNEL_STATUS_ASSIGNED):
        return "ASSIGNED"
    elif (channel_status == CHANNEL_STATUS_SEARCHING):
        return "SEARCHING"
    elif (channel_status == CHANNEL_STATUS_TRACKING):
        return "TRACKING"
    else:
        return "..."


#id_or_ids is single value or a list of values (trying to accommodate heart rate pages.....)
def data_page(name, id_or_ids, pack_format, arg_names):
    """
    Return a class supporting basic packing
    operations with the given metadata.
    """
    # pre-create the struct used to pack/unpack this message format
    if pack_format:
        byte_order_and_size = ""
        if pack_format[0] not in ("@", "=", "<", ">", "!"):
            # apply default by order and size
            byte_order_and_size = "<"
        dp_struct = struct.Struct(byte_order_and_size + pack_format)
    else:
        dp_struct = None

    # create named-tuple used to converting *arg, **kwds to this data page's args
    dp_arg_tuple = collections.namedtuple(name, arg_names)

    # class representing the data page definition passed to this method
    class DataPage(object):

        NAME        = name
        ID_or_IDs   = id_or_ids

        def __init__(self, *args, **kwds):
            tuple = dp_arg_tuple(*args, **kwds)
            self.__dict__.update(tuple._asdict())
            
            #Test if the supplied data page number is correct for this instance
            dpn = None
            try:
                dpn = self.data_page_number
            except Exception:
                pass
            try:
                dpn = self.data_page_number_and_page_change_toggle
            except Exception:
                pass
            is_valid = False
            if (dpn == self.ID_or_IDs):
                is_valid = True
            if not(is_valid):
                try:
                    if (dpn in self.ID_or_IDs):
                        is_valid = True
                except Exception, e:
                    pass
            if not(is_valid):
                raise AntDataPageNumberError("Value of " +str(dpn) + " is not valid for this data page " + str(self.ID_or_IDs))

        @property
        def args(self):
            return dp_arg_tuple(**dict((k, v) for k, v in self.__dict__.items() if k in arg_names))

        #Added as there is a name mismatch between Beacon and the Message type
        @classmethod
        def unpack(cls, packed_args):
            return cls.unpack_args(packed_args)

        @classmethod
        def unpack_args(cls, packed_args):
            try:
                is_valid = False
                dpn = ord(packed_args[0])
                if (dpn == cls.ID_or_IDs):
                    is_valid = True
                if not(is_valid):
                    try:
                        if (dpn in cls.ID_or_IDs):
                            is_valid = True
                    except Exception, e:
                        pass
                if (is_valid):
                    temp = DataPage(*dp_struct.unpack(packed_args))
                    return temp
                else:
                    return None
            except AttributeError:
                #This logic seems to differ between messages and Beacon......
                #return DataPage(*([None] * len(arg_names)))
                #Make easier/consistent
                return None

        #Added as there is a name mismatch between Beacon and the Message type
        def pack(self):
            return self.pack_args()

        def pack_args(self):
            try: return dp_struct.pack(*self.args)
            except AttributeError: pass
        
        def pack_size(self):
            try: return dp_struct.size
            except AttributeError: return 0

        def __str__(self):
            #TODO
            return str(self.args)

    return DataPage


DATA_PAGE_HEART_RATE_0              = 0x00
DATA_PAGE_HEART_RATE_0ALT           = 0x80
DATA_PAGE_HEART_RATE_1              = 0x01
DATA_PAGE_HEART_RATE_1ALT           = 0x81
DATA_PAGE_HEART_RATE_2              = 0x02
DATA_PAGE_HEART_RATE_2ALT           = 0x82
DATA_PAGE_HEART_RATE_3              = 0x03
DATA_PAGE_HEART_RATE_3ALT           = 0x83
DATA_PAGE_HEART_RATE_4              = 0x04
DATA_PAGE_HEART_RATE_4ALT           = 0x84

DATA_PAGE_REQUEST_DATA              = 0x46

DATA_PAGE_COMMON_MANUFACTURERS_INFO = 0x50


#Some bit field ops as structs don't deal with them nicely
#TODO -- Lookup if structs can easily be/has been extended to support bits
HeartRatePage0 = data_page("HeartRatePage0", [DATA_PAGE_HEART_RATE_0, DATA_PAGE_HEART_RATE_0ALT], "BBBBBBBB", \
                           ["data_page_number_and_page_change_toggle", "reserved1", "reserved2", "reserved3", "heart_beat_event_time_lsb", "heart_beat_event_time_msb", "heart_beat_count", "computed_heart_rate"])
HeartRatePage1 = data_page("HeartRatePage1", [DATA_PAGE_HEART_RATE_1, DATA_PAGE_HEART_RATE_1ALT], "BBBBBBBB", \
                           ["data_page_number_and_page_change_toggle", "cumulative_operating_time_lsb", "cumulative_operating_time_2ndlsb", "cumulative_operating_time_msb", "heart_beat_event_time_lsb", "heart_beat_event_time_msb", "heart_beat_count", "computed_heart_rate"])
HeartRatePage2 = data_page("HeartRatePage2", [DATA_PAGE_HEART_RATE_2, DATA_PAGE_HEART_RATE_2ALT], "BBBBBBBB", \
                           ["data_page_number_and_page_change_toggle", "manufacturer_id", "serial_number_lsb", "serial_number_msb", "heart_beat_event_time_lsb", "heart_beat_event_time_msb", "heart_beat_count", "computed_heart_rate"])
HeartRatePage3 = data_page("HeartRatePage3", [DATA_PAGE_HEART_RATE_3, DATA_PAGE_HEART_RATE_3ALT], "BBBBBBBB", \
                           ["data_page_number_and_page_change_toggle", "hardware_version", "software_version", "model_number", "heart_beat_event_time_lsb", "heart_beat_event_time_msb", "heart_beat_count", "computed_heart_rate"])
HeartRatePage4 = data_page("HeartRatePage4", [DATA_PAGE_HEART_RATE_4, DATA_PAGE_HEART_RATE_4ALT], "BBBBBBBB", \
                           ["data_page_number_and_page_change_toggle", "manufacturer_specific", "previous_heart_beat_event_lsb", "previous_heart_beat_event_msb", "heart_beat_event_time_lsb", "heart_beat_event_time_msb", "heart_beat_count", "computed_heart_rate"])

RequestDataPage = data_page("RequestDataPage", DATA_PAGE_REQUEST_DATA, "BBBBBBBB", \
                               ["data_page_number", "reserved1", "reserved2", "descriptor_byte1", "descriptor_byte2", "requested_transmission_response", "requested_page_number", "command_id"]) 


def decode_payload_data_str( data ): #TODO: Confirm name is correct
    #print "decode_payload_data_str : " + data.encode("hex")

    #Other pages don't have unpack
    from antd.antfs import Auth, Command, Beacon
    #Note that Auth must be before beacon as command can be a sub data type of beacon (it is processed/unpacked in a special way)
    temp = Auth.unpack(data)
    if temp:
        return str(temp)
    #Note that Command must be before beacon as command can be a sub data type of beacon (it is processed/unpacked in a special way)
    temp = Command.unpack(data)
    if temp:
        return str(temp)
    temp = Beacon.unpack(data)
    if temp:
        return str(temp)
    temp = HeartRatePage0.unpack(data)
    if temp:
        return str(temp)
    temp = HeartRatePage1.unpack(data)
    if temp:
        return str(temp)
    temp = HeartRatePage2.unpack(data)
    if temp:
        return str(temp)
    temp = HeartRatePage3.unpack(data)
    if temp:
        return str(temp)
    temp = HeartRatePage4.unpack(data)
    if temp:
        return str(temp)
    temp = RequestDataPage.unpack(data)
    if temp:
        return str(temp)

    return "..."


def message(direction, name, id, pack_format, arg_names, retry_policy=default_retry_policy, matcher=default_matcher, validator=default_validator):
    """
    Return a class supporting basic packing
    operations with the given metadata.
    """
    # pre-create the struct used to pack/unpack this message format
    if pack_format:
        byte_order_and_size = ""
        if pack_format[0] not in ("@", "=", "<", ">", "!"):
            # apply default by order and size
            byte_order_and_size = "<"
        msg_struct = struct.Struct(byte_order_and_size + pack_format)
    else:
        msg_struct = None

    # create named-tuple used to converting *arg, **kwds to this messages args
    msg_arg_tuple = collections.namedtuple(name, arg_names)

    # class representing the message definition passed to this method
    class Message(object):

        DIRECTION = direction
        NAME = name
        ID = id

        def __init__(self, *args, **kwds):
            tuple = msg_arg_tuple(*args, **kwds)
            self.__dict__.update(tuple._asdict())

        @property
        def args(self):
            return msg_arg_tuple(**dict((k, v) for k, v in self.__dict__.items() if k in arg_names))

        @classmethod
        def unpack_args(cls, packed_args):
            try: return Message(*msg_struct.unpack(packed_args))
            except AttributeError: return Message(*([None] * len(arg_names)))

        def pack_args(self):
            try: return msg_struct.pack(*self.args)
            except AttributeError: pass
        
        def pack_size(self):
            try: return msg_struct.size
            except AttributeError: return 0

        def is_retryable(self, err):
            return retry_policy(err)

        def is_reply(self, cmd):
            return matcher(self, cmd)

        def validate_reply(self, cmd):
            return validator(self, cmd)

        def __str__(self):
            import string
            #Create a human readable decoded form of this message object
            #yes -- it wastes cycles, but it helps me understand the flow much more quickly
            length = len(self.__dict__)
            count = 0
            temp_str = self.NAME + "("
            for (k, v) in self.__dict__.items():
                temp_str += str(k)+"="
                if (str(k) in ('data')) and not(self.ID == MESG_BURST_DATA_ID):
                    # Decode data as a data page in all but SEND/RECV_BURST_TRANSFER_PACKET
                    temp_str += "0x"+ v.encode("HEX") + " -> " + decode_payload_data_str(v)
                elif (str(k) in ('data')) and (self.ID == MESG_BURST_DATA_ID):
                    temp_str += "0x"+ v.encode("HEX") + " ?-> " + decode_payload_data_str(v)
                elif str(k) in ('msg_id'): #Lookup and have a nice human readable output. TODO: Bad form that key is here.....
                    temp_str += message_id_lookup(v) + "["+str(v)+"]"
                elif str(k) in ('msg_code'): #Lookup and have a nice human readable output. TODO: Bad form that key is here.....
                    temp_str += message_code_lookup(v) +"["+str(v)+"]"
                elif str(k) in ('channel_status'): #Lookup and have a nice human readable output. TODO: Bad form that key is here.....
                    temp_str += channel_status_lookup(v) +"["+str(v)+"]"
                elif isinstance(v, basestring): #and not(all(ord(c) < 127 and c in string.printable for c in v)):
                    #Not sure on all strings with hex data in them, but if they do then treat them as such...
                    temp_str += "0x"+ v.encode("HEX")
                else:
                    temp_str += str(v)
                if (count < (length-1)):
                    temp_str += ", " #If not last
                count += 1
            temp_str += ")"
            #Old
            #return str(self.args)
            return temp_str

    return Message

# ANT Message Protocol Definitions
UnassignChannel             = message(DIR_OUT, "UNASSIGN_CHANNEL", 0x41, "B", ["channel_number"], retry_policy=timeout_retry_policy)
AssignChannel               = message(DIR_OUT, "ASSIGN_CHANNEL", 0x42, "BBB", ["channel_number", "channel_type", "network_number"], retry_policy=timeout_retry_policy)
SetChannelId                = message(DIR_OUT, "SET_CHANNEL_ID", 0x51, "BHBB", ["channel_number", "device_number", "device_type_id", "trans_type"], retry_policy=timeout_retry_policy)
SetChannelPeriod            = message(DIR_OUT, "SET_CHANNEL_PERIOD", 0x43, "BH", ["channel_number", "messaging_period"], retry_policy=timeout_retry_policy) 
SetChannelSearchTimeout     = message(DIR_OUT, "SET_CHANNEL_SEARCH_TIMEOUT", 0x44, "BB", ["channel_number", "search_timeout"], retry_policy=timeout_retry_policy)
SetChannelRfFreq            = message(DIR_OUT, "SET_CHANNEL_RF_FREQ", 0x45, "BB", ["channel_number", "rf_freq"], retry_policy=timeout_retry_policy)
SetNetworkKey               = message(DIR_OUT, "SET_NETWORK_KEY", 0x46, "B8s", ["network_number", "network_key"], retry_policy=timeout_retry_policy)
ResetSystem                 = message(DIR_OUT, "RESET_SYSTEM", 0x4a, "x", [], retry_policy=always_retry_policy, matcher=reset_matcher)
OpenChannel                 = message(DIR_OUT, "OPEN_CHANNEL", 0x4b, "B", ["channel_number"], retry_policy=timeout_retry_policy)
CloseChannel                = message(DIR_OUT, "CLOSE_CHANNEL", 0x4c, "B", ["channel_number"], retry_policy=timeout_retry_policy, matcher=close_channel_matcher, validator=close_channel_validator)
RequestMessage              = message(DIR_OUT, "REQUEST_MESSAGE", 0x4d, "BB", ["channel_number", "msg_id"], retry_policy=timeout_retry_policy, matcher=request_message_matcher)
SetSearchWaveform           = message(DIR_OUT, "SET_SEARCH_WAVEFORM", 0x49, "BH", ["channel_number", "waveform"], retry_policy=timeout_retry_policy)
SendBroadcastData           = message(DIR_OUT, "SEND_BROADCAST_DATA", 0x4e, "B8s", ["channel_number", "data"], matcher=send_data_matcher, validator=send_data_validator)
SendAcknowledgedData        = message(DIR_OUT, "SEND_ACKNOWLEDGED_DATA", 0x4f, "B8s", ["channel_number", "data"], retry_policy=wait_and_retry_policy, matcher=send_data_matcher, validator=send_data_validator)
SendBurstTransferPacket     = message(DIR_OUT, "SEND_BURST_TRANSFER_PACKET", 0x50, "B8s", ["channel_number", "data"], retry_policy=wait_and_retry_policy, matcher=send_data_matcher, validator=send_data_validator)
StartupMessage              = message(DIR_IN, "STARTUP_MESSAGE", 0x6f, "B", ["startup_message"])
SerialError                 = message(DIR_IN, "SERIAL_ERROR", 0xae, None, ["error_number", "msg_contents"])
RecvBroadcastData           = message(DIR_IN, "RECV_BROADCAST_DATA", 0x4e, "B8s", ["channel_number", "data"])
RecvAcknowledgedData        = message(DIR_IN, "RECV_ACKNOWLEDGED_DATA", 0x4f, "B8s", ["channel_number", "data"])
RecvBurstTransferPacket     = message(DIR_IN, "RECV_BURST_TRANSFER_PACKET", 0x50, "B8s", ["channel_number", "data"])
ChannelEvent                = message(DIR_IN, "CHANNEL_EVENT", 0x40, "BBB", ["channel_number", "msg_id", "msg_code"])
ChannelStatus               = message(DIR_IN, "CHANNEL_STATUS", 0x52, "BB", ["channel_number", "channel_status"])
ChannelId                   = message(DIR_IN, "CHANNEL_ID", 0x51, "BHBB", ["channel_number", "device_number", "device_type_id", "man_id"])
AntVersion                  = message(DIR_IN, "VERSION", 0x3e, "11s", ["ant_version"])
SerialNumber                = message(DIR_IN, "SERIAL_NUMBER", 0x61, "I", ["serial_number"])
# Synthetic Commands
UnimplementedCommand        = message(None, "UNIMPLEMENTED_COMMAND", None, None, ["msg_id", "msg_contents"])

# hack, capabilities may be 4 (AP1) or 6 (AP2) bytes
class Capabilities(message(DIR_IN, "CAPABILITIES", 0x54, "BBBB", ["max_channels", "max_networks", "standard_opts", "advanced_opts1"])):

    @classmethod
    def unpack_args(cls, packed_args):
        return super(Capabilities, cls).unpack_args(packed_args[:4])


ALL_ANT_COMMANDS = [ UnassignChannel, AssignChannel, SetChannelId, SetChannelPeriod, SetChannelSearchTimeout,
                     SetChannelRfFreq, SetNetworkKey, ResetSystem, OpenChannel, CloseChannel, RequestMessage,
                     SetSearchWaveform, SendBroadcastData, SendAcknowledgedData, SendBurstTransferPacket,
                     StartupMessage, SerialError, RecvBroadcastData, RecvAcknowledgedData, RecvBurstTransferPacket,
                     ChannelEvent, ChannelStatus, ChannelId, AntVersion, Capabilities, SerialNumber ]

class ReadData(RequestMessage):
    """
    A phony command which is pushed to request data from client.
    This command will remain running as long as the channel is
    in a state where read is valid, and raise error if channel
    transitions to a state where read is impossible. Its kind-of
    an ugly hack so that channel status causes exceptions in read.
    """

    def __init__(self, channel_id, data_type):
        super(ReadData, self).__init__(channel_id, ChannelStatus.ID)
        self.data_type = data_type
    
    def is_retryable(self):
        return False

    def is_reply(self, cmd):
        return ((same_channel_or_network_matcher(self, cmd)
                    and isinstance(cmd, ChannelStatus)
                    and cmd.channel_status & 0x03 not in (CHANNEL_STATUS_SEARCHING, CHANNEL_STATUS_TRACKING))
                or close_channel_matcher(self, cmd))

    def validate_reply(self, cmd):
        return AntChannelClosedError("Channel closed. %s" % cmd)
    
    def __str__(self):
        return "ReadData(channel_number=%d)" % self.channel_number

class SendBurstData(SendBurstTransferPacket):

    data = None
    channel_number = None

    def __init__(self, channel_number, data):
        if len(data) <= 8: channel_number |= 0x80
        super(SendBurstData, self).__init__(channel_number, data)

    @property
    def done(self):
        return self._done

    @done.setter
    def done(self, value):
        self._done = value
        self.seq_num = 0
        self.index = 0
        self.incr_packet_index()

    def create_next_packet(self):
        """
        Return a command which can be exceuted
        to deliver the next packet of this burst.
        """
        is_last_packet = self.index + 8 >= len(self.data)
        data = self.data[self.index:self.index + 8]
        channel_number = self.channel_number | ((self.seq_num & 0x03) << 5) | (0x80 if is_last_packet else 0x00)
        return SendBurstTransferPacket(channel_number, data)
    
    def incr_packet_index(self):
        """
        Increment the pointer for data in next packet.
        create_next_packet() will update index until
        this method is called.
        """
        self.seq_num += 1
        if not self.seq_num & 0x03: self.seq_num += 1
        self.index += 8
        self.has_more_data = self.index < len(self.data)

    def __str__(self):
        return "SEND_BURST_COMMAND(channel_number=%d)" % self.channel_number


class Core(object):
    """
    Asynchronous ANT api.
    """
    
    def __init__(self, hardware, messages=ALL_ANT_COMMANDS):
        self.hardware = hardware
        self.input_msg_by_id = dict((m.ID, m) for m in messages if m.DIRECTION == DIR_IN)
        # per ant protocol doc, writing 15 zeros
        # should reset internal state of device.
        #self.hardware.write([0] * 15, 100)

    def close(self):
        self.hardware.close()

    def pack(self, command):
        """
        Return an array of bytes representing
        the data which needs to be written to
        hardware to execute the given command.
        """
        if command.ID is not None:
            if command.DIRECTION != DIR_OUT:
                _log.warning("Request to pack input message. %s", command)
            msg = [SYNC, command.pack_size(), command.ID]
            msg_args = command.pack_args()
            if msg_args is not None:
                msg.extend(array.array("B", msg_args))
                msg.append(generate_checksum(msg))
                return msg
    
    def unpack(self, msg):
        """
        Return the command represented by
        the given byte ANT array.
        """
        if not validate_checksum(msg):
            _log.error("Invalid checksum, message discarded. %s", msg_to_string(msg))
            return None
        sync, length, msg_id = msg[:3]
        try:
            command_class = self.input_msg_by_id[msg_id]
        except (KeyError):
            _log.warning("Attempt to unpack unknown message (0x%02x). %s", msg_id, msg_to_string(msg))
            return UnimplementedCommand(msg_id, msg)
        else:
            return command_class.unpack_args(array.array("B", msg[3:-1]).tostring())

    def send(self, command, timeout=100):
        """
        Execute the given command. Returns true
        if command was written to device. False
        if the device nack'd the write. When
        the method returns false, caller should
        retry.
        """
        msg = self.pack(command)
        if not msg: return True
        _trace.debug("SEND: %s", msg_to_string(msg))
        # ant protocol states \x00\x00 padding is optional.
        # libusb01 is quirky when using multiple threads?
        # adding the \00's seems to help with occasional issue
        # where read can block indefinitely until more data
        # is received.
        msg.extend([0] * 2)
        try:
            self.hardware.write(msg, timeout)
            return True
        except IOError as err:
            if is_timeout(err): return False
            else: raise

    def recv(self, timeout=1000):
        """
        A generator which return commands
        parsed from input stream of ant device.
        StopIteration raised when input stream empty.
        """
        while True:
            try:
                # tokenize message (possibly more than on per read)
                for msg in tokenize_message(self.hardware.read(timeout)):
                    _trace.debug("RECV: %s", msg_to_string(msg))
                    cmd = self.unpack(msg)
                    if cmd: yield cmd
            except IOError as err:
                # iteration terminates on timeout
                if is_timeout(err): raise StopIteration()
                else: raise


class Session(object):
    """
    Provides synchronous (blocking) API
    on top of basic (Core) ANT impl.
    """

    default_read_timeout = 5
    default_write_timeout = 5
    default_retry = 9

    channels = []
    networks = []
    _recv_buffer = []
    _burst_buffer = []

    def __init__(self, core):
        self.core = core
        self.running = False
        self.running_cmd = None
        try:
            self._start()
        except Exception as e:
            try: self.close()
            except Exception: _log.warning("Caught exception trying to cleanup resources.", exc_info=True)
            finally: raise e
    
    def _start(self):
        """
        Start the message consumer thread.
        """
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.loop)
            self.thread.daemon = True
            self.thread.start()
            self.reset_system()

    def close(self):
        """
        Stop the message consumer thread.
        """
        try:
            self.reset_system()
            self.running = False
            self.thread.join(1)
            self.core.close()
            assert not self.thread.is_alive()
        except AttributeError: pass

    def reset_system(self):
        """
        Reset the and device and initialize
        channel/network properties.
        """
        self._send(ResetSystem(), timeout=.5, retry=5)
        if not self.channels:
            _log.debug("Querying ANT capabilities")
            cap = self.get_capabilities() 
            #ver = self.get_ant_version()
            #sn = self.get_serial_number()
            _log.debug("Device Capabilities: %s", cap)
            #_log.debug("Device ANT Version: %s", ver)
            #_log.debug("Device SN#: %s", sn)
            self.channels = [Channel(self, n) for n in range(0, cap.max_channels)]
            self.networks = [Network(self, n) for n in range(0, cap.max_networks)]
        self._recv_buffer = [[]] * len(self.channels)
        self._burst_buffer = [[]] * len(self.channels)

    def get_capabilities(self):
        """
        Return the capabilities of this device. 9.5.7.4
        """
        return self._send(RequestMessage(0, Capabilities.ID))

    def get_ant_version(self):
        """
        Return the version on ANT firmware on device. 9.5.7.3
        """
        return self._send(RequestMessage(0, AntVersion.ID))

    def get_serial_number(self):
        """
        Return SN# of and device. 9.5.7.5
        """
        return self._send(RequestMessage(0, SerialNumber.ID))

    def _send(self, cmd, timeout=1, retry=0):
        """
        Execute the given command. An exception will
        be raised if reply is not received in timeout
        seconds. If retry is non-zero, commands returning
        EAGAIN will be retried. retry also appleis to
        RESET_SYSTEM commands. This method blocks until
        a response if received from hardware or timeout.
        Care should be taken to ensure timeout is sufficiently
        large. Care should be taken to ensure timeout is
        at least as large as a on message period.
        """
        _log.debug("Executing Command. %s", cmd)
        for t in range(0, retry + 1):
            # invalid to send command while another is running
            # (except for reset system)
            assert not self.running_cmd or isinstance(cmd, ResetSystem)
            # HACK, need to clean this up. not all devices support sending
            # a response message for ResetSystem, so don't bother waiting for it
            if not isinstance(cmd, ResetSystem):
                # set expiration and event on command. Once self.runnning_cmd
                # is set access to this command from this thread is invalid 
                # until event object is set.
                cmd.expiration = time.time() + timeout if timeout > 0 else None
                cmd.done = threading.Event()
                self.running_cmd = cmd
            else:
                # reset is done without waiting
                cmd.done = threading.Event()
                cmd.result = StartupMessage(0)
            # continue trying to commit command until session closed or command timeout 
            while self.running and not cmd.done.is_set() and not self.core.send(cmd):
                _log.warning("Device write timeout. Will keep trying.")
            if isinstance(cmd, ResetSystem):
                # sleep to give time for reset to execute
                time.sleep(1)
                cmd.done.set()
            # continue waiting for command completion until session closed
            while self.running and not cmd.done.is_set():
                if isinstance(cmd, SendBurstData) and cmd.has_more_data:
                    # if the command being executed is burst
                    # continue writing packets until data empty.
                    # usb will nack packed it case where we're
                    # overflowing the ant device. and packet will
                    # be tring next time.
                    packet = cmd.create_next_packet()
                    if self.core.send(packet): cmd.incr_packet_index()
                else:
                    cmd.done.wait(1)
            # cmd.done guarantees a result is available
            if cmd.done.is_set():
                try:
                    return cmd.result
                except AttributeError:
                    # must have failed, check if error is retryable
                    if t < retry and cmd.is_retryable(cmd.error):
                        _log.warning("Retryable error. %d try(s) remaining. %s", retry - t, cmd.error)
                    else:
                        # not retryable, or too many retries
                        raise cmd.error
            else:
                self.running_cmd = None
                raise AntError("Session closed.")

    def _handle_reply(self, cmd):
        """
        Handle the given command, updating
        the status of running command if
        applicable.
        """
        _log.debug("Processing reply. %s", cmd)
        if self.running_cmd and self.running_cmd.is_reply(cmd):
            err = self.running_cmd.validate_reply(cmd)
            if err:
                self._set_error(err)
            else:
                self._set_result(cmd)

    def _handle_timeout(self):
        """
        Update the status of running command
        if the message has expired.
        """
        # if a command is currently running, check for timeout condition
        if self.running_cmd and self.running_cmd.expiration and time.time() > self.running_cmd.expiration:
            self._set_error(AntTimeoutError("No reply to command. %s" %  self.running_cmd))

    def _handle_read(self, cmd=None):
        """
        Append incoming ack messages to read buffer.
        Append completed burst message to buffer.
        Full run command from buffer if data available.
        """
        # handle update the recv buffers
        try:
            # acknowledged data is immediately made avalible to client
            # (and buffered if no read is currently running)
            if isinstance(cmd, RecvAcknowledgedData):
                self._recv_buffer[cmd.channel_number].append(cmd)
            # burst data double-buffered. it is not made available to
            # client until the complete transfer is completed.
            elif isinstance(cmd, RecvBurstTransferPacket):
                channel_number = 0x1f & cmd.channel_number
                self._burst_buffer[channel_number].append(cmd)
                # burst complete, make the complete burst available for read.
                if cmd.channel_number & 0x80:
                    _log.debug("Burst transfer completed, marking %d packets available for read.", len(self._burst_buffer[channel_number]))
                    self._recv_buffer[channel_number].extend(self._burst_buffer[channel_number])
                    self._burst_buffer[channel_number] = []
            # a burst transfer failed, any data currently read is discarded.
            # we assume the sender will retransmit the entire payload.
            elif isinstance(cmd, ChannelEvent) and cmd.msg_id == 1 and cmd.msg_code == EVENT_TRANSFER_RX_FAILED:
                _log.warning("Burst transfer failed, discarding data. %s", cmd)
                self._burst_buffer[cmd.channel_number] = []
        except IndexError:
            _log.warning("Ignoring data, buffers not initialized. %s", cmd)

        # dispatcher data if running command is ReadData and something available
        if self.running_cmd and isinstance(self.running_cmd, ReadData):
            if isinstance(cmd, RecvBroadcastData) and self.running_cmd.data_type == RecvBroadcastData:
                # read broadcast is unbuffered, and blocks until a broadcast is received
                # if a broadcast is received and nobody is listening it is discarded.
                self._set_result(cmd)
            elif self._recv_buffer[self.running_cmd.channel_number]:
                if self.running_cmd.data_type == RecvAcknowledgedData:
                    # return the most recent acknowledged data packet if one exists
                    for ack_msg in [msg for msg in self._recv_buffer[self.running_cmd.channel_number] if isinstance(msg, RecvAcknowledgedData)]:
                        self._set_result(ack_msg)
                        self._recv_buffer[self.running_cmd.channel_number].remove(ack_msg)
                        break
                elif self.running_cmd.data_type in (RecvBurstTransferPacket, ReadData):
                    # select in a single entire burst transfer or ACK
                    data = []
                    for pkt in list(self._recv_buffer[self.running_cmd.channel_number]):
                        if isinstance(pkt, RecvBurstTransferPacket) or self.running_cmd.data_type == ReadData:
                            data.append(pkt)
                            self._recv_buffer[self.running_cmd.channel_number].remove(pkt)
                            if pkt.channel_number & 0x80 or isinstance(pkt, RecvAcknowledgedData): break
                    # append all text to data of first packet
                    if data:
                        result = data[0]
                        for pkt in data[1:]:
                            result.data += pkt.data
                        self._set_result(result)

    def _handle_log(self, msg):
        if isinstance(msg, ChannelEvent) and msg.msg_id == 1:
            if msg.msg_code == EVENT_RX_SEARCH_TIMEOUT:
                _log.warning("RF channel timed out searching for device. channel_number=%d", msg.channel_number)
            elif msg.msg_code == EVENT_RX_FAIL:
                _log.warning("Failed to receive RF beacon at expected period. channel_number=%d", msg.channel_number)
            elif msg.msg_code == EVENT_RX_FAIL_GO_TO_SEARCH:
                _log.warning("Channel dropped to search do to too many dropped messages. channel_number=%d", msg.channel_number)
            elif msg.msg_code == EVENT_CHANNEL_COLLISION:
                _log.warning("Channel collision, another RF device intefered with channel. channel_number=%d", msg.channel_number)
            elif msg.msg_code == EVENT_SERIAL_QUE_OVERFLOW:
                _log.error("USB Serial buffer overflow. PC reading too slow.")

    def _set_result(self, result):
        """
        Update the running command with given result,
        and set flag to indicate to caller that command
        is done.
        """
        if self.running_cmd:
            cmd = self.running_cmd
            self.running_cmd = None
            cmd.result = result
            cmd.done.set()

    def _set_error(self, err):
        """
        Update the running command with 
        given exception. The exception will
        be raised to thread which invoked 
        synchronous command.
        """
        if self.running_cmd:
            cmd = self.running_cmd
            self.running_cmd = None
            cmd.error = err
            cmd.done.set()

    def loop(self):
        """
        Message loop consuming data from the
        ANT device. Typically loop is started
        by thread created in Session.open()
        """
        try:
            while self.running:
                for cmd in self.core.recv():
                    if not self.running: break
                    self._handle_log(cmd)
                    self._handle_read(cmd)
                    self._handle_reply(cmd)
                    self._handle_timeout()
                else:
                    if not self.running: break
                    self._handle_read()
                    self._handle_timeout()
        except Exception:
            _log.error("Caught Exception handling message, session closing.", exc_info=True)
        finally:
            self.running_cmd = None
            self.running = False


class Channel(object):

    def __init__(self, session, channel_number):
        self._session = session;
        self.channel_number = channel_number

    def open(self):
        self._session._send(OpenChannel(self.channel_number))

    def close(self):
        self._session._send(CloseChannel(self.channel_number))

    def assign(self, channel_type, network_number):
        self._session._send(AssignChannel(self.channel_number, channel_type, network_number))

    def unassign(self):
        self._session._send(UnassignChannel(self.channel_number))

    def set_id(self, device_number=0, device_type_id=0, trans_type=0):
        self._session._send(SetChannelId(self.channel_number, device_number, device_type_id, trans_type))

    def set_period(self, messaging_period=8192):
        self._session._send(SetChannelPeriod(self.channel_number, messaging_period))

    def set_search_timeout(self, search_timeout=12):
        self._session._send(SetChannelSearchTimeout(self.channel_number, search_timeout))

    def set_rf_freq(self, rf_freq=66):
        self._session._send(SetChannelRfFreq(self.channel_number, rf_freq))

    def set_search_waveform(self, search_waveform=None):
        if search_waveform is not None:
            self._session._send(SetSearchWaveform(self.channel_number, search_waveform))

    def get_status(self):
        return self._session._send(RequestMessage(self.channel_number, ChannelStatus.ID))

    def get_id(self):
        return self._session._send(RequestMessage(self.channel_number, ChannelId.ID))

    def send_broadcast(self, data, timeout=None):
        if timeout is None: timeout = self._session.default_write_timeout
        data = data_tostring(data)
        assert len(data) <= 8
        self._session._send(SendBroadcastData(self.channel_number, data), timeout=timeout)

    def send_acknowledged(self, data, timeout=None, retry=None, direct=False):
        if timeout is None: timeout = self._session.default_write_timeout
        if retry is None: retry = self._session.default_retry
        data = data_tostring(data)
        assert len(data) <= 8
        cmd = SendAcknowledgedData(self.channel_number, data)
        if not direct:
            self._session._send(cmd, timeout=timeout, retry=retry)
        else:
            # force message tx regardless of command queue
            # state, and ignore result. usefully for best
            # attempt cleanup on exit.
            self._session.core.send(cmd)

    def send_burst(self, data, timeout=None, retry=None):
        if timeout is None: timeout = self._session.default_write_timeout
        if retry is None: retry = self._session.default_retry
        data = data_tostring(data)
        self._session._send(SendBurstData(self.channel_number, data), timeout=timeout, retry=retry)

    def recv_broadcast(self, timeout=None, returnMessageNotData=False):
        if timeout is None: timeout = self._session.default_read_timeout
        return self._session._send(ReadData(self.channel_number, RecvBroadcastData), timeout=timeout).data

    def recv_acknowledged(self, timeout=None):
        if timeout is None: timeout = self._session.default_read_timeout
        return self._session._send(ReadData(self.channel_number, RecvAcknowledgedData), timeout=timeout).data

    def recv_burst(self, timeout=None):
        if timeout is None: timeout = self._session.default_read_timeout
        return self._session._send(ReadData(self.channel_number, RecvBurstTransferPacket), timeout=timeout).data 

    def write(self, data, timeout=None, retry=None):
        if timeout is None: timeout = self._session.default_write_timeout
        if retry is None: retry = self._session.default_retry
        data = data_tostring(data)
        if len(data) <= 8:
            self.send_acknowledged(data, timeout=timeout, retry=retry)
        else:
            self.send_burst(data, timeout=timeout, retry=retry)
    
    def read(self, timeout=None):
        if timeout is None: timeout = self._session.default_read_timeout
        return self._session._send(ReadData(self.channel_number, ReadData), timeout=timeout).data 
    
class Network(object):

    def __init__(self, session, network_number):
        self._session = session
        self.network_number = network_number

    def set_key(self, network_key="\x00" * 8):
        self._session._send(SetNetworkKey(self.network_number, network_key))


# vim: ts=4 sts=4 et
