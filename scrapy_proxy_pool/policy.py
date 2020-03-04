# -*- coding: utf-8 -*-

import re

from scrapy.exceptions import IgnoreRequest


class BanDetectionPolicy(object):
    """ Default ban detection rules. """
    NOT_BAN_STATUSES = {200, 301, 302, 404, 500}
    NOT_BAN_EXCEPTIONS = (IgnoreRequest,)
    BANNED_PATTERN = re.compile(r'(Captive Portal|SESSION EXPIRED)', re.IGNORECASE)

    def response_is_ban(self, request, response):
        if self.BANNED_PATTERN.search(response.text):
            return True

        if response.status not in self.NOT_BAN_STATUSES:
            return True
        if response.status == 200 and not len(response.body):
            return True

        return False

    def exception_is_ban(self, request, exception):
        return not isinstance(exception, self.NOT_BAN_EXCEPTIONS)
