"""
# -*- coding: utf-8 -*-
# ===============================================================================
#
# Copyright (C) 2013/2017 Laurent Labatut / Laurent Champagnac
#
#
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA
# ===============================================================================
"""

import logging

import gevent
import urllib3
from gevent.threading import Lock
from gevent.timeout import Timeout
from geventhttpclient.client import PROTO_HTTPS, HTTPClient
from geventhttpclient.url import URL
from pysolbase.SolBase import SolBase
from urllib3 import PoolManager, ProxyManager, Retry

from pysolhttpclient.Http.HttpResponse import HttpResponse

logger = logging.getLogger(__name__)

# Suppress warnings
urllib3.disable_warnings()


class HttpClient(object):
    """
    Http client
    """

    HTTP_IMPL_AUTO = None
    HTTP_IMPL_GEVENT = 1
    HTTP_IMPL_URLLIB3 = 3

    def __init__(self):
        """
        Const
        """

        # Gevent
        self._gevent_pool_max = 1024
        self._gevent_locker = Lock()
        self._gevent_pool = dict()

        # urllib3
        # Force underlying fifo queue to 1024 via maxsize
        self._u3_basic_pool = PoolManager(num_pools=1024, maxsize=1024)
        self._u3_proxy_pool_max = 1024
        self._u3_proxy_locker = Lock()
        self._u3_proxy_pool = dict()

    # ====================================
    # GEVENT HTTP POOL
    # ====================================

    def gevent_from_pool(self, url, http_request):
        """
        Get a gevent client from url and request
        :param url: URL
        :type url: URL
        :param http_request: HttpRequest
        :type http_request: HttpRequest
        :return HTTPClient
        :rtype HTTPClient
        """

        # Compute key
        key = "{0}#{1}#{2}#{3}#{4}#{5}#{6}#{7}#{8}#{9}#".format(
            # host and port
            url.host,
            url.port,
            # Ssl
            url.scheme == PROTO_HTTPS,
            # Other dynamic stuff
            http_request.https_insecure,
            http_request.disable_ipv6,
            http_request.connection_timeout_ms / 1000,
            http_request.network_timeout_ms / 1000,
            http_request.http_concurrency,
            http_request.http_proxy_host,
            http_request.http_proxy_port,
        )

        # Check
        if key in self._gevent_pool:
            SolBase.sleep(0)
            return self._gevent_pool[key]

        # Allocate (in lock)
        with self._gevent_locker:
            # Check maxed
            if len(self._gevent_pool) >= self._gevent_pool_max:
                raise Exception("gevent pool maxed, cur={0}, max={1}".format(
                    len(self._gevent_pool), self._gevent_pool_max
                ))

            # Ok, allocate
            http = HTTPClient.from_url(
                url,
                insecure=http_request.https_insecure,
                disable_ipv6=http_request.disable_ipv6,
                connection_timeout=http_request.connection_timeout_ms / 1000,
                network_timeout=http_request.network_timeout_ms / 1000,
                concurrency=http_request.http_concurrency,
                proxy_host=http_request.http_proxy_host,
                proxy_port=http_request.http_proxy_port,
                headers={},
            )

            self._gevent_pool[key] = http
            logger.info("Started new pool for key=%s", key)
            SolBase.sleep(0)
            return http

    # ====================================
    # URLLIB3 HTTP PROXY POOL
    # ====================================

    def urllib3_from_pool(self, http_request):
        """
        Get a u3 pool from url and request
        :param http_request: HttpRequest
        :type http_request: HttpRequest
        :return Object
        :rtype Object
        """

        if not http_request.http_proxy_host:
            SolBase.sleep(0)
            return self._u3_basic_pool

        # Compute key
        key = "{0}#{1}#".format(
            http_request.http_proxy_host,
            http_request.http_proxy_port,
        )

        # Check
        if key in self._u3_proxy_pool:
            SolBase.sleep(0)
            return self._u3_proxy_pool[key]

        # Allocate (in lock)
        with self._u3_proxy_locker:
            # Check maxed
            if len(self._u3_proxy_pool) >= self._u3_proxy_pool_max:
                raise Exception("u3 pool maxed, cur={0}, max={1}".format(
                    len(self._u3_proxy_pool), self._u3_proxy_pool_max
                ))

            # Uri
            proxy_url = "http://{0}:{1}".format(
                http_request.http_proxy_host,
                http_request.http_proxy_port)

            # Ok, allocate
            # Force underlying fifo queue to 1024 via maxsize
            p = ProxyManager(num_pools=1024, maxsize=1024, proxy_url=proxy_url)
            self._u3_proxy_pool[key] = p
            logger.info("Started new pool for key=%s", key)
            SolBase.sleep(0)
            return p

    # ====================================
    # HTTP EXEC
    # ====================================

    def go_http(self, http_request):
        """
        Perform an http request
        :param http_request: HttpRequest
        :type http_request: HttpRequest
        :return HttpResponse
        :rtype HttpResponse
        """

        ms = SolBase.mscurrent()
        http_response = HttpResponse()
        general_timeout_sec = float(http_request.general_timeout_ms) / 1000.0
        try:
            # Assign request
            http_response.http_request = http_request

            # Fire
            gevent.with_timeout(
                general_timeout_sec,
                self._go_http_internal,
                http_request, http_response)
            SolBase.sleep(0)
        except Timeout:
            # Failed
            http_response.exception = Exception("Timeout while processing, general_timeout_sec={0}".format(general_timeout_sec))
        finally:
            # Switch
            SolBase.sleep(0)
            # Assign ms
            http_response.elapsed_ms = SolBase.msdiff(ms)
            # Log it
            logger.info("Http call over, general_timeout_sec=%.2f, resp=%s, req=%s", general_timeout_sec, http_response, http_request)

        # Return
        return http_response

    def _go_http_internal(self, http_request, http_response):
        """
        Perform an http request
        :param http_request: HttpRequest
        :type http_request: HttpRequest
        :param http_response: HttpResponse
        :type http_response: HttpResponse
        """

        try:
            # Default to gevent
            impl = http_request.force_http_implementation
            if impl == HttpClient.HTTP_IMPL_AUTO:
                # Fallback gevent (urllib3 issue with latest uwsgi, gevent 1.1.1)
                impl = HttpClient.HTTP_IMPL_URLLIB3
                # impl = HttpClient.HTTP_IMPL_GEVENT

            # Uri
            url = URL(http_request.uri)
            SolBase.sleep(0)

            # If proxy and https => urllib3
            if http_request.http_proxy_host and url.scheme == PROTO_HTTPS:
                # Fallback gevent (urllib3 issue with latest uwsgi, gevent 1.1.1)
                impl = HttpClient.HTTP_IMPL_URLLIB3
                # impl = HttpClient.HTTP_IMPL_GEVENT

            # Log
            logger.debug("Http using impl=%s", impl)

            # Fire
            if impl == HttpClient.HTTP_IMPL_GEVENT:
                self._go_gevent(http_request, http_response)
                SolBase.sleep(0)
            elif impl == HttpClient.HTTP_IMPL_URLLIB3:
                self._go_urllib3(http_request, http_response)
                SolBase.sleep(0)
            else:
                raise Exception("Invalid force_http_implementation")
        except Exception as e:
            logger.warn("Ex=%s", SolBase.extostr(e))
            http_response.exception = e
            raise

    # ====================================
    # MISC
    # ====================================

    @classmethod
    def _add_header(cls, d, k, v):
        """
        Add header k,v to d
        :param d: dict
        :type d: dict
        :param k: header key
        :param k: str
        :param v: header value
        :param v: str
        """

        if k not in d:
            d[k] = v
        else:
            # Already present
            if isinstance(d[k], list):
                # Just append
                d[k].append(v)
            else:
                # Build a list, existing value and new value
                d[k] = [d[k], v]

    # ====================================
    # GEVENT
    # ====================================

    def _go_gevent(self, http_request, http_response):
        """
        Perform an http request
        :param http_request: HttpRequest
        :type http_request: HttpRequest
        :param http_response: HttpResponse
        :type http_response: HttpResponse
        """

        # Implementation
        http_response.http_implementation = HttpClient.HTTP_IMPL_GEVENT

        # Uri
        url = URL(http_request.uri)
        SolBase.sleep(0)

        # Patch for path attribute error
        try:
            _ = url.path
        except AttributeError:
            url.path = "/"

        # Get instance
        logger.debug("Get pool")
        http = self.gevent_from_pool(url, http_request)
        logger.debug("Get pool done, pool=%s", http)
        SolBase.sleep(0)

        # Fire
        ms_start = SolBase.mscurrent()
        logger.debug("Http now")
        if not http_request.method:
            # ----------------
            # Auto-detect
            # ----------------
            if http_request.post_data:
                # Post
                response = http.post(url.request_uri,
                                     body=http_request.post_data,
                                     headers=http_request.headers)
            else:
                # Get
                response = http.get(url.request_uri,
                                    headers=http_request.headers)
        else:
            # ----------------
            # Use input
            # ----------------
            if http_request.method == "GET":
                response = http.get(url.request_uri,
                                    headers=http_request.headers)
            elif http_request.method == "DELETE":
                response = http.delete(url.request_uri,
                                       body=http_request.post_data,
                                       headers=http_request.headers)
            elif http_request.method == "HEAD":
                response = http.head(url.request_uri,
                                     headers=http_request.headers)
            elif http_request.method == "PUT":
                response = http.put(url.request_uri,
                                    body=http_request.post_data,
                                    headers=http_request.headers)
            elif http_request.method == "POST":
                response = http.post(url.request_uri,
                                     body=http_request.post_data,
                                     headers=http_request.headers)
            elif http_request.method == "PATCH":
                raise Exception("Unsupported gevent method={0}".format(http_request.method))
            elif http_request.method == "OPTIONS":
                raise Exception("Unsupported gevent method={0}".format(http_request.method))
            elif http_request.method == "TRACE":
                raise Exception("Unsupported gevent method={0}".format(http_request.method))
            else:
                raise Exception("Invalid gevent method={0}".format(http_request.method))

        logger.debug("Http done, ms=%s", SolBase.msdiff(ms_start))
        SolBase.sleep(0)

        # Check
        if not response:
            raise Exception("No response from http")

        # Process it
        http_response.status_code = response.status_code

        # Read
        ms_start = SolBase.mscurrent()
        logger.debug("Read now")
        http_response.buffer = response.read()
        SolBase.sleep(0)
        logger.debug("Read done, ms=%s", SolBase.msdiff(ms_start))
        if response.content_length:
            http_response.content_length = response.content_length
        else:
            if http_response.buffer:
                http_response.content_length = len(http_response.buffer)
            else:
                http_response.content_length = 0

        # noinspection PyProtectedMember
        for k, v in response._headers_index.iteritems():
            HttpClient._add_header(http_response.headers, k, v)

        response.should_close()

        # Over
        SolBase.sleep(0)

    # ====================================
    # URLLIB3
    # ====================================

    def _go_urllib3(self, http_request, http_response):
        """
        Perform an http request
        :param http_request: HttpRequest
        :type http_request: HttpRequest
        :param http_response: HttpResponse
        :type http_response: HttpResponse
        """

        # Implementation
        http_response.http_implementation = HttpClient.HTTP_IMPL_URLLIB3

        # Get pool
        logger.debug("From pool")
        cur_pool = self.urllib3_from_pool(http_request)
        logger.debug("From pool ok")
        SolBase.sleep(0)

        # From pool
        logger.debug("From pool2")
        if http_request.http_proxy_host:
            # ProxyManager : direct
            conn = cur_pool
        else:
            # Get connection from basic pool
            conn = cur_pool.connection_from_url(http_request.uri)
        logger.debug("From pool2 ok")
        SolBase.sleep(0)

        # Retries
        retries = Retry(total=0,
                        connect=0,
                        read=0,
                        redirect=0)
        SolBase.sleep(0)

        # Fire
        logger.debug("urlopen")
        if not http_request.method:
            # ----------------
            # Auto-detect
            # ----------------
            if http_request.post_data:
                r = conn.urlopen(
                    method='POST',
                    url=http_request.uri,
                    body=http_request.post_data,
                    headers=http_request.headers,
                    redirect=False,
                    retries=retries,
                )
            else:
                r = conn.urlopen(
                    method='GET',
                    url=http_request.uri,
                    headers=http_request.headers,
                    redirect=False,
                    retries=retries,
                )
        else:
            # ----------------
            # Use input
            # ----------------
            if http_request.method in ["GET", "HEAD", "OPTIONS", "TRACE"]:
                r = conn.urlopen(
                    method=http_request.method,
                    url=http_request.uri,
                    headers=http_request.headers,
                    redirect=False,
                    retries=retries,
                )
            elif http_request.method in ["POST", "PUT", "PATCH", "DELETE"]:
                r = conn.urlopen(
                    method=http_request.method,
                    url=http_request.uri,
                    body=http_request.post_data,
                    headers=http_request.headers,
                    redirect=False,
                    retries=retries,
                )
            else:
                raise Exception("Invalid urllib3 method={0}".format(http_request.method))
        logger.debug("urlopen ok")
        SolBase.sleep(0)

        # Ok
        http_response.status_code = r.status
        for k, v in r.headers.iteritems():
            HttpClient._add_header(http_response.headers, k, v)
        http_response.buffer = r.data
        http_response.content_length = len(http_response.buffer)

        # Over
        SolBase.sleep(0)
