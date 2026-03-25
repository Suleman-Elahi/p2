"""p2 logging"""

from collections import ChainMap
from time import time
from uuid import uuid4

import logging

LOGGER = logging.getLogger(__name__)

class LogAdaptor:
    """Cache logged data and add log method to request"""

    __cache = {}

    def start_request(self, request):
        """Add unique ID to request and create logging method"""
        request.uid = uuid4().hex

        def request_logger(**kwargs):
            if request.uid not in self.__cache:
                self.__cache[request.uid] = []
            self.__cache[request.uid].append(kwargs)
        request.log = request_logger
        request.log(
            uid=request.uid,
            start_time=time())
        LOGGER.bind(request_id=request.uid)

    def end_request(self, request):
        """Flatten logged data and create Record"""
        request.log(end_time=time())
        flattened = ChainMap(*self.__cache[request.uid])
        flattened['duration'] = flattened['end_time'] - flattened['start_time']
        flattened['user'] = request.user.pk
        flattened['source_address'] = request.META['REMOTE_ADDR']
        flattened['request_path'] = request.path
        if hasattr(request, 'resolver_match') and request.resolver_match:
            flattened['app'] = request.resolver_match.app_name
        # write_log_record.delay(dict(flattened))


LOG_ADAPTOR = LogAdaptor()
