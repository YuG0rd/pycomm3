import logging
from itertools import tee, zip_longest, cycle
from reprlib import repr as _r
from typing import Dict, Any, Optional, Sequence, Union

from .ethernetip import SendUnitDataRequestPacket, SendUnitDataResponsePacket
from .util import (parse_read_reply,  get_service_status, request_path,
                   get_extended_status, tag_request_path)

from ..bytes_ import Pack, Unpack
from ..cip import CLASS_TYPE, INSTANCE_TYPE, DataType, ClassCode, Services, DataTypeSize
from ..const import INSUFFICIENT_PACKETS, STRUCTURE_READ_REPLY, SUCCESS
from ..exceptions import RequestError


class TagServiceResponsePacket(SendUnitDataResponsePacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')

    def __init__(self, request: 'TagServiceRequestPacket', raw_data: bytes = None):
        self.tag = request.tag
        self.elements = request.elements
        self.tag_info = request.tag_info
        super().__init__(request, raw_data)


class TagServiceRequestPacket(SendUnitDataRequestPacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    response_class = TagServiceResponsePacket
    tag_service = None

    def __init__(self, tag: str, elements: int, tag_info: Dict[str, Any],
                 request_id: int, use_instance_id: bool = True):
        super().__init__()
        self.tag = tag
        self.elements = elements
        self.tag_info = tag_info
        self.request_id = request_id
        self._use_instance_id = use_instance_id
        self.request_path = None


class ReadTagResponsePacket(TagServiceResponsePacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')

    def __init__(self, request: 'ReadTagRequestPacket', raw_data: bytes = None):
        self.value = None
        self.data_type = None
        super().__init__(request, raw_data)

    def _parse_reply(self, dont_parse=False):
        try:
            super()._parse_reply()
            if self.is_valid() and not dont_parse:
                self.value, self.data_type = parse_read_reply(self.data, self.tag_info, self.elements)
        except Exception as err:
            self.__log.exception('Failed parsing reply data')
            self.value = None
            self._error = f'Failed to parse reply - {err}'

    def __repr__(self):
        return f'{self.__class__.__name__}({self.data_type!r}, {_r(self.value)}, {self.service_status!r})'


# TODO: remove the request_path arg, the path should be created in the request
#       it was originally, but then moved outside to make packet size estimation more accurate
#       but, multi packet will be changed to accept packets and can then use the full message
#       from the tag packet for tracking size

class ReadTagRequestPacket(TagServiceRequestPacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    type_ = 'read'
    response_class = ReadTagResponsePacket
    tag_service = Services.read_tag

    def _setup_message(self):
        super()._setup_message()
        if self.request_path is None:
            self.request_path = tag_request_path(self.tag, self.tag_info, self._use_instance_id)
            if self.request_path is None:
                self.error = f'Failed to build request path for tag'
        self._msg += [self.tag_service, self.request_path, Pack.uint(self.elements)]

    #
    # def build_request(self, target_cid: bytes, session_id: int, context: bytes, option: int,
    #                   sequence: cycle = None, **kwargs):
    #
    #
    #     return super().build_request(target_cid, session_id, context, option, sequence, **kwargs)


class ReadTagFragmentedResponsePacket(ReadTagResponsePacket):
    # TODO
    __log = logging.getLogger(f'{__module__}.{__qualname__}')

    def __init__(self, request: 'ReadTagFragmentedRequestPacket', raw_data: bytes = None):
        self.value = None
        self._data_type = None
        self.value_bytes = None

        super().__init__(request, raw_data)

    def _parse_reply(self):
        super()._parse_reply(dont_parse = True)
        if self.data[:2] == STRUCTURE_READ_REPLY:
            self.value_bytes = self.data[4:]
            self._data_type = self.data[:4]
        else:
            self.value_bytes = self.data[2:]
            self._data_type = self.data[:2]

    def parse_value(self):
        try:
            if self.is_valid():
                self.value, self.data_type = parse_read_reply(self._data_type + self.value_bytes,
                                                              self.request.tag_info, self.request.elements)
            else:
                self.value, self.data_type = None, None
        except Exception as err:
            self.__log.exception('Failed parsing reply data')
            self.value = None
            self._error = f'Failed to parse reply - {err}'

    def __repr__(self):
        return f'{self.__class__.__name__}(raw_data={_r(self.raw)})'

    __str__ = __repr__


class ReadTagFragmentedRequestPacket(ReadTagRequestPacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    type_ = 'read'
    response_class = ReadTagFragmentedResponsePacket
    tag_service = Services.read_tag_fragmented

    def __init__(self, tag: str, elements: int, tag_info: Dict[str, Any],
                 request_id: int, use_instance_id: bool = True, offset: int = 0):
        super().__init__(tag, elements, tag_info, request_id, use_instance_id)
        self.offset = offset

    def _setup_message(self):
        super()._setup_message()
        self._msg.append(Pack.udint(self.offset))

    @classmethod
    def from_request(cls, request: Union[ReadTagRequestPacket, 'ReadTagFragmentedRequestPacket'], offset=0) -> 'ReadTagFragmentedRequestPacket':
        new_request = cls(
            request.tag,
            request.elements,
            request.tag_info,
            request.request_id,
            request._use_instance_id,
            offset
        )
        new_request.request_path = request.request_path

        return new_request



    def send(self):
        # TODO: determine best approach here, will probably require more work in the
        #       driver send method to handle the fragmenting
        if not self.error:
            offset = 0
            responses = []
            while offset is not None:
                self._msg.extend([Services.read_tag_fragmented,
                                  self.request_path,
                                  Pack.uint(self.elements),
                                  Pack.dint(offset)])
                self._send(self.build_request())
                self.__log.debug(f'Sent: {self!r} (offset={offset})')
                reply = self._receive()
                response = ReadTagFragmentedResponsePacket(reply, self.tag_info, self.elements)
                self.__log.debug(f'Received: {response!r}')
                responses.append(response)
                if response.service_status == INSUFFICIENT_PACKETS:
                    offset += len(response.bytes_)
                    self._msg = [Pack.uint(self._driver._sequence)]
                else:
                    offset = None
            if all(responses):
                final_response = responses[-1]
                final_response.bytes_ = b''.join(resp.bytes_ for resp in responses)
                final_response.parse_bytes()
                self.__log.debug(f'Reassembled Response: {final_response!r}')
                return final_response

        failed_response = ReadTagResponsePacket()
        failed_response._error = self.error or 'One or more fragment responses failed'
        self.__log.debug(f'Reassembled Response: {failed_response!r}')
        return failed_response

    def __repr__(self):
        return f'{self.__class__.__name__}(tag={self.tag!r}, elements={self.elements!r})'


class WriteTagResponsePacket(TagServiceResponsePacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')


class WriteTagRequestPacket(TagServiceRequestPacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    type_ = 'write'
    response_class = WriteTagResponsePacket
    tag_service = Services.write_tag

    def __init__(self, tag: str, elements: int, tag_info: Dict[str, Any], request_id: int,
                 use_instance_id: bool = True, value: bytes = b'', request_path: Optional[bytes] = None):
        super().__init__(tag, elements, tag_info, request_id, use_instance_id)
        self.value = value
        self.data_type = tag_info['data_type_name']
        self._packed_data_type = None

        if request_path:
            self.request_path = request_path
        else:
            self.request_path = tag_request_path(tag, tag_info, use_instance_id)

        if self.request_path is None:
            self.error = 'Failed to create request path for tag'
        else:
            # if bits_write:
            #     request_path = make_write_data_bit(tag_info, value, self.request_path)
            #     data_type = 'BOOL'
            # else:
            #     request_path, data_type = make_write_data_tag(tag_info, value, elements, self.request_path)

            if tag_info['tag_type'] == 'struct':
                if not isinstance(value, bytes):
                    raise RequestError('Writing UDTs only supports bytes for value')
                self._packed_data_type = b'\xA0\x02' + Pack.uint(tag_info['data_type']['template']['structure_handle'])

            elif self.data_type not in DataType:
                raise RequestError(f"Unsupported data type: {self.data_type!r}")
            else:
                self._packed_data_type = Pack.uint(DataType[self.data_type])

            self._msg += [
                self.tag_service,
                self.request_path,
                self._packed_data_type,
                Pack.uint(elements),
                value
            ]

    def __repr__(self):
        return f'{self.__class__.__name__}(tag={self.tag!r}, value={_r(self.value)}, elements={self.elements!r})'


class WriteTagFragmentedResponsePacket(WriteTagResponsePacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')


class WriteTagFragmentedRequestPacket(WriteTagRequestPacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    type_ = 'write'
    response_class = WriteTagFragmentedResponsePacket
    tag_service = Services.write_tag_fragmented

    @classmethod
    def from_request(cls, request: WriteTagRequestPacket) -> 'WriteTagFragmentedRequestPacket':
        return cls(
            request.tag,
            request.elements,
            request.tag_info,
            request.request_id,
            request._use_instance_id,
            request.value,
        )

    # def __init__(self, tag: str, elements: int, tag_info: Dict[str, Any], request_id: int, request_path, value):
    #     super().__init__(tag, elements, tag_info, request_id, request_path, value)
    #     self.request_path = request_path
    #     self.segment_size = None
    #     self.data_type = tag_info['data_type_name']
    #
    #     try:
    #         if tag_info['tag_type'] == 'struct':
    #             self._packed_type = STRUCTURE_READ_REPLY + Pack.uint(
    #                 tag_info['data_type']['template']['structure_handle'])
    #         else:
    #             self._packed_type = Pack.uint(DataType[self.data_type])
    #
    #         if self.request_path is None:
    #             self.error = 'Invalid Tag Request Path'
    #     except Exception as err:
    #         self.__log.exception('Failed adding request')
    #         self.error = err

    def send(self):
        if not self.error:
            responses = []
            segment_size = self._driver.connection_size - (len(self.request_path) + len(self._packed_type)
                                                           + 9)  # 9 = len of other stuff in the path

            pack_func = Pack[self.data_type] if self.tag_info['tag_type'] == 'atomic' else lambda x: x
            segments = (self.value[i: i +segment_size]
                        for i in range(0, len(self.value), segment_size))

            offset = 0
            elements_packed = Pack.uint(self.elements)

            for i, segment in enumerate(segments, start=1):
                segment_bytes = b''.join(pack_func(s) for s in segment) if not isinstance(segment, bytes) else segment
                self._msg.extend((
                    Services.write_tag_fragmented,
                    self.request_path,
                    self._packed_type,
                    elements_packed,
                    Pack.dint(offset),
                    segment_bytes
                ))

                self._send(self.build_request())
                self.__log.debug(f'Sent: {self!r} (part={i} offset={offset})')
                reply = self._receive()
                response = WriteTagFragmentedResponsePacket(reply)
                self.__log.debug(f'Received: {response!r}')
                responses.append(response)
                offset += len(segment_bytes)
                self._msg = [Pack.uint(self._driver._sequence), ]

            if all(responses):
                final_response = responses[-1]
                self.__log.debug(f'Reassembled Response: {final_response!r}')
                return final_response

        failed_response = WriteTagFragmentedResponsePacket()
        failed_response._error = self.error or 'One or more fragment responses failed'
        self.__log.debug(f'Reassembled Response: {failed_response!r}')
        return failed_response


class ReadModifyWriteResponsePacket(WriteTagResponsePacket):
    ...


class ReadModifyWriteRequestPacket(SendUnitDataRequestPacket):

    # TODO: remove the bits_write from write tag, this class will replace those
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    type_ = 'write'
    response_class = ReadModifyWriteResponsePacket
    tag_service = Services.read_modify_write

    def __init__(self, tag: str, tag_info: Dict[str, Any], request_id: int, use_instance_id: bool = True,):
        super().__init__()
        self.tag = tag
        self.value = None
        self.elements = 0
        self.tag_info = tag_info
        self.request_id = request_id
        self._use_instance_id = use_instance_id
        self.data_type = tag_info['data_type_name']
        self.request_path = tag_request_path(tag, tag_info, use_instance_id)
        self.bits = []
        self._request_ids = []
        self._and_mask = 0xFFFF_FFFF_FFFF_FFFF
        self._or_mask = 0x0000_0000_0000_0000
        self._mask_size = DataTypeSize.get(self.data_type)

        if self._mask_size is None:
            raise RequestError(f'Invalid data type {tag_info["data_type"]} for writing bits')

        if self.request_path is None:
            self.error = 'Failed to create request path for tag'

    def set_bit(self, bit: int, value: bool, request_id: int):
        if self.data_type == 'DWORD':
            bit = bit % 32

        if value:
            self._or_mask |= (1 << bit)
            self._and_mask |= (1 << bit)
        else:
            self._or_mask &= ~(1 << bit)
            self._and_mask &= ~(1 << bit)

        self.bits.append(bit)
        self._request_ids.append(request_id)

    def build_request(self, target_cid: bytes, session_id: int, context: bytes, option: int,
                      sequence: cycle = None, **kwargs):

        self._msg = [
            self.tag_service,
            self.request_path,
            Pack.uint(self._mask_size),
            Pack.ulint(self._or_mask)[:self._mask_size],
            Pack.ulint(self._and_mask)[:self._and_mask]
        ]

        return super().build_request(target_cid, session_id, context, option, sequence, **kwargs)


class MultiServiceResponsePacket(SendUnitDataResponsePacket):
    __log = logging.getLogger(f'{__module__}.{__qualname__}')

    def __init__(self, request: 'MultiServiceRequestPacket', raw_data: bytes = None):
        self.request = request
        self.values = None
        self.request_statuses = None
        self.responses = []
        super().__init__(request, raw_data)

    def _parse_reply(self):
        super()._parse_reply()
        num_replies = Unpack.uint(self.data)
        offset_data = self.data[2:2 + 2 * num_replies]
        offsets = (Unpack.uint(offset_data[i:i+2]) for i in range(0, len(offset_data), 2))
        start, end = tee(offsets)  # split offsets into start/end indexes
        next(end)   # advance end by 1 so 2nd item is the end index for the first item
        reply_data = [self.data[i:j] for i, j in zip_longest(start, end)]

        padding = bytes(46)   # pad the front of the packet so it matches the size of
                              # a read tag response, probably not the best idea but it works for now

        for data, request in zip(reply_data, self.request.requests):
            response = request.response_class(request, padding + data)
            self.responses.append(response)
        #     service = data[0:1]
        #     service_status = data[2]
        #     tag['service_status'] = service_status
        #     if service_status != SUCCESS:
        #         tag['error'] = f'{get_service_status(service_status)} - {get_extended_status(data, 2)}'
        #
        #     if Services.get(Services.from_reply(service)) == Services.read_tag:
        #         if service_status == SUCCESS:
        #             value, dt = parse_read_reply(data[4:], tag['tag_info'], tag['elements'])
        #         else:
        #             value, dt = None, None
        #
        #         values.append(value)
        #         tag['value'] = value
        #         tag['data_type'] = dt
        #     else:
        #         tag['value'] = None
        #         tag['data_type'] = None
        #
        # self.values = values

    def __repr__(self):
        return f'{self.__class__.__name__}(values={_r(self.values)}, error={self.error!r})'


class MultiServiceRequestPacket(SendUnitDataRequestPacket):
    # TODO:  this class should wrap the other tag request packets
    #        the add method should take other requests instead of builing them itself
    __log = logging.getLogger(f'{__module__}.{__qualname__}')
    type_ = 'multi'
    response_class = MultiServiceResponsePacket

    def __init__(self, requests: Sequence[TagServiceRequestPacket]):
        super().__init__()
        self.requests = requests
        self.request_path = request_path(ClassCode.message_router, 1)

        # self._msg.extend((
        #     Services.multiple_service_request,  # the Request Service
        #     Pack.usint(2),  # the Request Path Size length in word
        #     CLASS_TYPE["8-bit"],
        #     ClassCode.message_router,
        #     INSTANCE_TYPE["8-bit"],
        #     b'\x01',  # Instance 1
        # ))

    def _setup_message(self):
        super()._setup_message()
        self._msg += [Services.multiple_service_request, self.request_path]

    def build_message(self):
        super().build_message()
        num_requests = len(self.requests)
        self._msg.append(Pack.uint(num_requests))
        offset = 2 + (num_requests * 2)
        offsets = []

        messages = []
        for request in self.requests:
            request._setup_message()
            messages.append(b''.join((request.tag_service, request.request_path, Pack.uint(request.elements))))

        for msg in messages:
            offsets.append(Pack.uint(offset))
            offset += len(msg)

        return b''.join(self._msg + offsets + messages)

    # def build_message(self, tags):
    #     rp_list, errors = [], []
    #     for tag in tags:
    #         if tag['rp'] is None:
    #             errors.append(f'Unable to create request path {tag["tag"]}')
    #         else:
    #             rp_list.append(tag['rp'])
    #
    #     offset = len(rp_list) * 2 + 2
    #     offsets = []
    #     for rp in rp_list:
    #         offsets.append(Pack.uint(offset))
    #         offset += len(rp)
    #
    #     msg = self._msg + [Pack.uint(len(rp_list))] + offsets + rp_list
    #     return b''.join(msg)

    # def add_read(self, tag, request_path, elements, tag_info, request_id):
    #     # TODO: maybe instead of these methods, the multi-packet uses multiple normal ReadRequests
    #     #       and combines them as needed
    #     if request_path is not None:
    #         rp = Services.read_tag + request_path + Pack.uint(elements)
    #         _tag = {
    #             'tag': tag,
    #             'elements': elements,
    #             'tag_info': tag_info,
    #             'rp': rp,
    #             'service': 'read',
    #             'request_id': request_id
    #         }
    #         message = self.build_message(self.tags + [_tag])
    #         if len(message) < self._driver.connection_size:
    #             self._message = message
    #             self.tags.append(_tag)
    #             return True
    #         else:
    #             return False
    #     else:
    #         self.__log.error(f'Failed to create request path for {tag}')
    #         raise RequestError('Failed to create request path')
    #
    # def add_write(self, tag, request_path, value, elements, tag_info, request_id, bits_write=None):
    #     if request_path is not None:
    #         if bits_write:
    #             data_type = tag_info['data_type']
    #             request_path = make_write_data_bit(tag_info, value, request_path)
    #         else:
    #             request_path, data_type = make_write_data_tag(tag_info, value, elements, request_path)
    #
    #         _tag = {'tag': tag, 'elements': elements, 'tag_info': tag_info, 'rp': request_path, 'service': 'write',
    #                 'value': value, 'data_type': data_type, 'request_id': request_id}
    #
    #         message = self.build_message(self.tags + [_tag])
    #         if len(message) < self._driver.connection_size:
    #             self._message = message
    #             self.tags.append(_tag)
    #             return True
    #         else:
    #             return False
    #
    #     else:
    #         self.__log.error(f'Failed to create request path for {tag}')
    #         raise RequestError('Failed to create request path')
    #
    # def send(self):
    #     if not self._msg_errors:
    #         request = self.build_request()
    #         self._send(request)
    #         self.__log.debug(f'Sent: {self!r}')
    #         reply = self._receive()
    #         response = MultiServiceResponsePacket(reply, tags=self.tags)
    #     else:
    #         self.error = f'Failed to create request path for: {", ".join(self._msg_errors)}'
    #         response = MultiServiceResponsePacket()
    #         response._error = self.error
    #
    #     self.__log.debug(f'Received: {response!r}')
    #     return response







