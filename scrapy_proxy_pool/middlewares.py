# -*- coding: utf-8 -*-
from __future__ import absolute_import
import logging

from scrapy.exceptions import CloseSpider, NotConfigured
from scrapy import signals
from scrapy.utils.misc import load_object
from twisted.internet import task

from proxyscrape import create_collector


logger = logging.getLogger(__name__)


class ProxyPoolMiddleware(object):
    """
    Scrapy downloader middleware which choses a random proxy for each request.

    To enable it, add it and BanDetectionMiddleware
    to DOWNLOADER_MIDDLEWARES option::

        DOWNLOADER_MIDDLEWARES = {
            # ...
            'scrapy_proxy_pool.middlewares.ProxyPoolMiddleware': 610,
            'scrapy_proxy_pool.middlewares.BanDetectionMiddleware': 620,
            # ...
        }

    It keeps track of dead and alive proxies and avoids using dead proxies.
    Proxy is considered dead if request.meta['_ban'] is True, and alive
    if request.meta['_ban'] is False; to set this meta key use
    BanDetectionMiddleware.

    Dead proxies are re-checked with a randomized exponential backoff.

    By default, all default Scrapy concurrency options (DOWNLOAD_DELAY,
    AUTHTHROTTLE_..., CONCURRENT_REQUESTS_PER_DOMAIN, etc) become per-proxy
    for proxied requests when RotatingProxyMiddleware is enabled.
    For example, if you set CONCURRENT_REQUESTS_PER_DOMAIN=2 then
    spider will be making at most 2 concurrent connections to each proxy.

    Settings:

    * ``PROXY_POOL_FILTER_ANONYMOUS`` - whether to use anonymous proxy,
      False by default;
    * ``PROXY_POOL_FILTER_TYPES`` - which proxy types to use, only 'http' and 'https' is available.
      ['http', 'https'] by default;
    * ``PROXY_POOL_FILTER_CODE`` - which proxy country code to use.
      'us' by default;
    * ``PROXY_POOL_REFRESH_INTERVAL`` - proxies refresh interval in seconds,
      900 by default;
    * ``PROXY_POOL_LOGSTATS_INTERVAL`` - stats logging interval in seconds,
      30 by default;
    * ``PROXY_POOL_CLOSE_SPIDER`` - When True, spider is stopped if
      there are no alive proxies. If False (default), then when there is no
      alive proxies all dead proxies are re-checked.
    * ``PROXY_POOL_PAGE_RETRY_TIMES`` - a number of times to retry
      downloading a page using a different proxy. After this amount of retries
      failure is considered a page failure, not a proxy failure.
      Think of it this way: every improperly detected ban cost you
      ``PROXY_POOL_PAGE_RETRY_TIMES`` alive proxies. Default: 5.
    """
    def __init__(self, filters, refresh_interval, logstats_interval, stop_if_no_proxies,
                 max_proxies_to_try, force_refresh_if_no_proxies, try_with_host):
        self.collector = create_collector('proxy-pool', ['http', 'https'], refresh_interval)
        self.collector.apply_filter(filters)

        self.refresh_interval = refresh_interval
        self.logstats_interval = logstats_interval
        self.stop_if_no_proxies = stop_if_no_proxies
        self.max_proxies_to_try = max_proxies_to_try
        self.force_refresh_if_no_proxies = force_refresh_if_no_proxies
        self.try_with_host = try_with_host

    @classmethod
    def from_crawler(cls, crawler):
        s = crawler.settings
        enabled = s.getbool('PROXY_POOL_ENABLED', False)
        if not enabled:
            raise NotConfigured()

        filters = dict()
        filters['anonymous'] = s.getbool('PROXY_POOL_FILTER_ANONYMOUS', False)
        filters['type'] = s.get('PROXY_POOL_FILTER_TYPES', ['http', 'https'])
        filters['code'] = s.get('PROXY_POOL_FILTER_CODE', 'us')

        mw = cls(
            filters=filters,
            refresh_interval=s.getfloat('PROXY_POOL_REFRESH_INTERVAL', 600),
            logstats_interval=s.getfloat('PROXY_POOL_LOGSTATS_INTERVAL', 30),
            stop_if_no_proxies=s.getbool('PROXY_POOL_CLOSE_SPIDER', False),
            max_proxies_to_try=s.getint('PROXY_POOL_PAGE_RETRY_TIMES', 5),
            force_refresh_if_no_proxies=s.getbool('PROXY_POOL_FORCE_REFRESH', False),
            try_with_host=s.getbool('PROXY_POOL_TRY_WITH_HOST', True)
        )
        crawler.signals.connect(mw.engine_started,
                                signal=signals.engine_started)
        crawler.signals.connect(mw.engine_stopped,
                                signal=signals.engine_stopped)
        return mw

    def engine_started(self):
        self.log_task = task.LoopingCall(self.log_stats)
        self.log_task.start(self.logstats_interval, now=True)
        self.refresh_proxies_task = task.LoopingCall(self.refresh_blacklist)
        self.refresh_proxies_task.start(self.refresh_interval, now=False)

    def engine_stopped(self):
        if self.log_task.running:
            self.log_task.stop()
        if self.refresh_proxies_task.running:
            self.refresh_proxies_task.stop()

    def process_request(self, request, spider):
        if 'proxy' in request.meta and not request.meta.get('_PROXY_POOL', False):
            return
        proxy = self.collector.get_proxy()
        if not proxy:
            if self.stop_if_no_proxies:
                raise CloseSpider("no_proxies")
            else:
                logger.warn("No proxies available.")
                if self.force_refresh_if_no_proxies:
                    self.collector.refresh_proxies(True)
                    logger.info('Proxies refreshed.')
                    self.refresh_blacklist()

                logger.info("Try to download with host ip.")
                request.meta.pop('proxy_source', None)
                request.meta.pop('proxy', None)
                request.meta.pop('download_slot', None)
                request.meta.pop('_PROXY_POOL', None)
                return

        request.meta['proxy_source'] = proxy
        request.meta['proxy'] = '{}://{}:{}'.format(proxy.type, proxy.host, proxy.port)
        request.meta['download_slot'] = self.get_proxy_slot(proxy)
        request.meta['_PROXY_POOL'] = True

        logger.debug('[ProxyChoosen] {}'.format(request.meta['proxy']))

    def refresh_blacklist(self):
        self.collector.clear_blacklist()
        logger.info('Blacklist is cleared.')

    def get_proxy_slot(self, proxy):
        """
        Return downloader slot for a proxy.
        By default it doesn't take port in account, i.e. all proxies with
        the same hostname / ip address share the same slot.
        """
        return proxy.host

    def process_exception(self, request, exception, spider):
        return self._handle_result(request, spider)

    def process_response(self, request, response, spider):
        return self._handle_result(request, spider) or response

    def _handle_result(self, request, spider):
        proxy = request.meta.get('proxy_source', None)
        if not (proxy and request.meta.get('_PROXY_POOL', False)):
            return

        ban = request.meta.get('_ban', None)
        if ban is True:
            self.collector.blacklist_proxy(request.meta.get('proxy_source'))
            request.meta.pop('proxy_source', None)
            request.meta.pop('proxy', None)
            request.meta.pop('download_slot', None)
            request.meta.pop('_PROXY_POOL', None)
            return self._retry(request, spider)

    def _retry(self, request, spider):
        retries = request.meta.get('proxy_retry_times', 0) + 1
        max_proxies_to_try = request.meta.get('max_proxies_to_try',
                                              self.max_proxies_to_try)

        if retries <= max_proxies_to_try:
            logger.debug("Retrying %(request)s with another proxy "
                         "(failed %(retries)d times, "
                         "max retries: %(max_proxies_to_try)d)",
                         {'request': request, 'retries': retries,
                          'max_proxies_to_try': max_proxies_to_try},
                         extra={'spider': spider})
            retryreq = request.copy()
            retryreq.meta['proxy_retry_times'] = retries
            retryreq.dont_filter = True
            return retryreq
        else:
            logger.debug("Gave up retrying %(request)s (failed %(retries)d "
                         "times with different proxies)",
                         {'request': request, 'retries': retries},
                         extra={'spider': spider})

            if self.try_with_host:
                logger.debug("Try with host ip")
                req = request.copy()
                req.meta.pop('proxy_source', None)
                req.meta.pop('download_slot', None)
                req.meta.pop('_PROXY_POOL', None)
                req.meta['proxy'] = None
                req.dont_filter = True
                return req

    def log_stats(self):
        pass


class BanDetectionMiddleware(object):
    """
    Downloader middleware for detecting bans. It adds
    '_ban': True to request.meta if the response was a ban.

    To enable it, add it to DOWNLOADER_MIDDLEWARES option::

        DOWNLOADER_MIDDLEWARES = {
            # ...
            'scrapy_proxy_pool.middlewares.BanDetectionMiddleware': 620,
            # ...
        }

    By default, client is considered banned if a request failed, and alive
    if a response was received. You can override ban detection method by
    passing a path to a custom BanDectionPolicy in
    ``PROXY_POOL_BAN_POLICY``, e.g.::

    PROXY_POOL_BAN_POLICY = 'myproject.policy.MyBanPolicy'

    The policy must be a class with ``response_is_ban``
    and ``exception_is_ban`` methods. These methods can return True
    (ban detected), False (not a ban) or None (unknown). It can be convenient
    to subclass and modify default BanDetectionPolicy::

        # myproject/policy.py
        from rotating_proxies.policy import BanDetectionPolicy

        class MyPolicy(BanDetectionPolicy):
            def response_is_ban(self, request, response):
                # use default rules, but also consider HTTP 200 responses
                # a ban if there is 'captcha' word in response body.
                ban = super(MyPolicy, self).response_is_ban(request, response)
                ban = ban or b'captcha' in response.body
                return ban

            def exception_is_ban(self, request, exception):
                # override method completely: don't take exceptions in account
                return None

    Instead of creating a policy you can also implement ``response_is_ban``
    and ``exception_is_ban`` methods as spider methods, for example::

        class MySpider(scrapy.Spider):
            # ...

            def response_is_ban(self, request, response):
                return b'banned' in response.body

            def exception_is_ban(self, request, exception):
                return None

    """
    def __init__(self, stats, policy):
        self.stats = stats
        self.policy = policy

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.stats, cls._load_policy(crawler))

    @classmethod
    def _load_policy(cls, crawler):
        policy_path = crawler.settings.get(
            'PROXY_POOL_BAN_POLICY',
            'scrapy_proxy_pool.policy.BanDetectionPolicy'
        )
        policy_cls = load_object(policy_path)
        if hasattr(policy_cls, 'from_crawler'):
            return policy_cls.from_crawler(crawler)

        return policy_cls()

    def process_response(self, request, response, spider):
        is_ban = getattr(spider, 'response_is_ban',
                         self.policy.response_is_ban)
        ban = is_ban(request, response)
        request.meta['_ban'] = ban
        if ban:
            self.stats.inc_value("bans/status/%s" % response.status)
            if not len(response.body):
                self.stats.inc_value("bans/empty")
        return response

    def process_exception(self, request, exception, spider):
        is_ban = getattr(spider, 'exception_is_ban',
                         self.policy.exception_is_ban)
        ban = is_ban(request, exception)
        if ban:
            ex_class = "%s.%s" % (exception.__class__.__module__,
                                  exception.__class__.__name__)
            self.stats.inc_value("bans/error/%s" % ex_class)
        request.meta['_ban'] = ban
